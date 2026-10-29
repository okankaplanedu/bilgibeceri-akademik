#!/usr/bin/env python3
"""Bilgi Beceri • güvenli, bağımsız standart-kütüphane akademik arşiv üreticisi.

Kaynak kodu, kullanıcının FOMM aracındaki OpenAlex arama akışını sunucuya taşır;
Crossref, arXiv ve Semantic Scholar da ayrı gerçek adaptörlerdir. API anahtarları
sadece ortam değişkenlerinden okunur; üretilen web dosyalarına yazılmaz.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
LOGGER = logging.getLogger("bilgibeceri")
PROVIDER_NAMES = {"openalex": "OpenAlex", "crossref": "Crossref", "arxiv": "arXiv", "semanticscholar": "Semantic Scholar"}
SAFE_CATEGORY_ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def text(value: Any, limit: int = 1200) -> str:
    if value is None:
        return ""
    value = html.unescape(str(value))
    value = re.sub(r"<[^>]*>", " ", value)
    value = "".join(ch for ch in value if ch.isprintable() or ch in "\r\n\t")
    return re.sub(r"\s+", " ", value).strip()[:limit]


def list_names(authors: list[Any]) -> str:
    values = []
    for a in authors or []:
        if isinstance(a, str):
            name = a
        elif isinstance(a, dict):
            nested = a.get("author")
            if isinstance(nested, dict):
                name = nested.get("display_name", "")
            else:
                name = a.get("name") or a.get("display_name") or " ".join(
                    [str(a.get("given") or ""), str(a.get("family") or "")]
                )
        else:
            continue
        name = text(name, 160)
        if name:
            values.append(name)
    return "; ".join(values[:20])


def valid_year(raw: Any) -> str:
    try:
        year = int(raw)
    except (TypeError, ValueError):
        return ""
    return str(year) if 1400 <= year <= 2200 else ""


def safe_url(url: Any) -> str:
    s = text(url, 2000)
    if not s or any(c in s for c in '<>"\'\\'):
        return ""
    try:
        p = urllib.parse.urlsplit(s)
        if p.scheme.lower() not in ("http", "https") or not p.netloc or p.username or p.password:
            return ""
        if p.scheme.lower() == "http" and (p.hostname or "").lower() in ("arxiv.org", "export.arxiv.org"):
            return urllib.parse.urlunsplit(("https", p.netloc, p.path, p.query, p.fragment))
        return s
    except ValueError:
        return ""


def normalized_doi(raw: Any) -> str:
    s = text(raw, 400)
    s = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", s, flags=re.I)
    s = re.sub(r"^doi:\s*", "", s, flags=re.I)
    s = urllib.parse.unquote(s).strip().lower()
    if re.fullmatch(r"10\.\d{4,9}/[^\s<>\"']+", s, re.I):
        return s
    return ""


def slugify(value: str) -> str:
    value = value.translate(str.maketrans({"ı": "i", "İ": "I", "ş": "s", "Ş": "S", "ğ": "g", "Ğ": "G", "ç": "c", "Ç": "C", "ö": "o", "Ö": "O", "ü": "u", "Ü": "U"}))
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", value)).strip("-")[:60].strip("-") or "yayin"


def fingerprint_keys(record: dict[str, Any]) -> list[str]:
    result = []
    doi = normalized_doi(record.get("doi"))
    if doi:
        result.append("doi:" + doi)
    arxiv = text(record.get("arxiv_id"), 200).lower()
    if arxiv:
        result.append("arxiv:" + re.sub(r"v\d+$", "", arxiv))
    provider_id = text(record.get("provider_id"), 500)
    provider = text(record.get("provider_key"), 50)
    if provider_id and provider:
        result.append("source:" + provider + ":" + provider_id.lower())
    title = unicodedata.normalize("NFKC", text(record.get("title"), 1200)).casefold()
    title = re.sub(r"[^\w]+", "", title, flags=re.UNICODE)
    if len(title) >= 24:
        result.append("title:" + hashlib.sha256(title.encode("utf-8")).hexdigest()[:24])
    return result


def normalize(raw: dict[str, Any], category: dict[str, Any], query: str, provider: str) -> dict[str, Any] | None:
    title = text(raw.get("title"), 1200)
    if not title or title.casefold() in ("none", "untitled", "başlıksız"):
        return None
    doi = normalized_doi(raw.get("doi"))
    source_url = safe_url(raw.get("url"))
    if doi:
        source_url = "https://doi.org/" + urllib.parse.quote(doi, safe="/:;()[]-._~")
    if not source_url:
        return None
    r = {
        "title": title,
        "authors": text(raw.get("authors"), 2000),
        "year": valid_year(raw.get("year")),
        "doi": doi,
        "source_url": source_url,
        "provider_id": text(raw.get("provider_id"), 500),
        "arxiv_id": text(raw.get("arxiv_id"), 200),
        "provider_key": provider,
        "provider": PROVIDER_NAMES[provider],
        "venue": text(raw.get("venue"), 500),
        "work_type": text(raw.get("work_type"), 100),
        "category_id": category["id"],
        "category": text(category["name"], 120),
        "context": text(category.get("context") or category["name"], 400),
        "query": text(query, 240),
        "tokens": [text(x, 80) for x in category.get("tokens", [])[:12] if text(x, 80)],
        "created_at": now_iso(),
    }
    r["dedup_keys"] = fingerprint_keys(r)
    if not r["dedup_keys"]:
        return None
    return r


def request_bytes(endpoint: str, params: dict[str, Any], cfg: dict[str, Any], provider: str) -> bytes:
    url = endpoint + "?" + urllib.parse.urlencode(params)
    headers = {"User-Agent": "BilgiBeceriAcademicArchive/1.0"}
    email = text(cfg.get("contact_email"), 200)
    if email and re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        headers["User-Agent"] += " (mailto:" + email + ")"
    if provider == "openalex" and os.environ.get("OPENALEX_API_KEY"):
        headers["Authorization"] = "Bearer " + os.environ["OPENALEX_API_KEY"]
    if provider == "semanticscholar" and os.environ.get("SEMANTIC_SCHOLAR_API_KEY"):
        headers["x-api-key"] = os.environ["SEMANTIC_SCHOLAR_API_KEY"]
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=cfg["timeout_seconds"]) as response:
        # Bir yanıt en çok 8 MB: bozulan endpointin makineyi doldurmasını önler.
        body = response.read(8_000_001)
        if len(body) > 8_000_000:
            raise ValueError("API yanıtı beklenenden büyük")
        return body


def crossref_year(item: dict[str, Any]) -> str:
    for key in ("published", "published-print", "published-online", "issued", "created"):
        value = item.get(key) or {}
        parts = value.get("date-parts") or []
        if parts and parts[0] and valid_year(parts[0][0]):
            return valid_year(parts[0][0])
    return ""


def fetch_openalex(q: str, cfg: dict[str, Any]) -> list[dict[str, Any]]:
    params = {"search": q, "per_page": cfg["results_per_query_per_source"], "sort": "publication_date:desc"}
    data = json.loads(request_bytes("https://api.openalex.org/works", params, cfg, "openalex"))
    result = []
    for item in data.get("results", []):
        loc = item.get("primary_location") or {}
        source = loc.get("source") or {}
        result.append({"title": item.get("title"), "authors": list_names(item.get("authorships", [])),
                       "year": item.get("publication_year"), "doi": item.get("doi"),
                       "url": item.get("doi") or item.get("id"), "provider_id": item.get("id"),
                       "venue": source.get("display_name"), "work_type": item.get("type")})
    return result


def fetch_crossref(q: str, cfg: dict[str, Any]) -> list[dict[str, Any]]:
    params = {"query.bibliographic": q, "rows": cfg["results_per_query_per_source"], "sort": "published", "order": "desc"}
    email = text(cfg.get("contact_email"), 200)
    if email and re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        params["mailto"] = email
    data = json.loads(request_bytes("https://api.crossref.org/works", params, cfg, "crossref"))
    result = []
    for item in (data.get("message") or {}).get("items", []):
        title = item.get("title") or []
        venue = item.get("container-title") or []
        result.append({"title": title[0] if title else "", "authors": list_names(item.get("author", [])),
                       "year": crossref_year(item), "doi": item.get("DOI"), "url": item.get("URL"),
                       "provider_id": item.get("DOI"), "venue": venue[0] if venue else "", "work_type": item.get("type")})
    return result


def fetch_arxiv(q: str, cfg: dict[str, Any]) -> list[dict[str, Any]]:
    # Sorgu özel işlev/yol değil, Atom API'nin all alanında bir cümledir.
    safe_query = re.sub(r'["\\]', ' ', q)
    params = {"search_query": 'all:"' + safe_query + '"', "start": 0,
              "max_results": cfg["results_per_query_per_source"], "sortBy": "submittedDate", "sortOrder": "descending"}
    data = request_bytes("https://export.arxiv.org/api/query", params, cfg, "arxiv")
    root = ET.fromstring(data)
    ns = {"a": "http://www.w3.org/2005/Atom", "ax": "http://arxiv.org/schemas/atom"}
    result = []
    for item in root.findall("a:entry", ns):
        url = item.findtext("a:id", default="", namespaces=ns)
        arxiv_id = url.rsplit("/", 1)[-1]
        authors = [x.findtext("a:name", default="", namespaces=ns) for x in item.findall("a:author", ns)]
        published = item.findtext("a:published", default="", namespaces=ns)
        doi = item.findtext("ax:doi", default="", namespaces=ns)
        result.append({"title": item.findtext("a:title", default="", namespaces=ns), "authors": list_names(authors),
                       "year": published[:4], "doi": doi, "url": url, "provider_id": url,
                       "arxiv_id": arxiv_id, "venue": "arXiv", "work_type": "preprint"})
    return result


def fetch_semanticscholar(q: str, cfg: dict[str, Any]) -> list[dict[str, Any]]:
    params = {"query": q, "limit": cfg["results_per_query_per_source"],
              "fields": "title,year,authors,url,externalIds,venue,publicationTypes"}
    data = json.loads(request_bytes("https://api.semanticscholar.org/graph/v1/paper/search", params, cfg, "semanticscholar"))
    result = []
    for item in data.get("data", []):
        ids = item.get("externalIds") or {}
        result.append({"title": item.get("title"), "authors": list_names(item.get("authors", [])),
                       "year": item.get("year"), "doi": ids.get("DOI"), "url": item.get("url"),
                       "provider_id": item.get("paperId") or item.get("url"), "arxiv_id": ids.get("ArXiv"),
                       "venue": item.get("venue"), "work_type": ", ".join(item.get("publicationTypes") or [])})
    return result

FETCHERS = {"openalex": fetch_openalex, "crossref": fetch_crossref, "arxiv": fetch_arxiv, "semanticscholar": fetch_semanticscholar}


def _vb_literal_part(s: str) -> str:
    # Bu işlev parçanın sınırındaki boşlukları korumalıdır.
    chunks = s.split('%>')
    return ' & Chr(37) & ">" & '.join('"' + c.replace('"', '""') + '"' for c in chunks)


def vb_literal(value: Any) -> str:
    """ASP sonlandırıcısı %> ve VBScript tırnakları veri içinden kaçamaz."""
    return _vb_literal_part(text(value, 4000))


def vb_assignment(name: str, value: Any) -> str:
    """VBScript'in ~1022 karakterlik statik satır sınırını aşmadan metni birleştir."""
    s = text(value, 4000)
    chunks = [s[i:i + 60] for i in range(0, len(s), 60)] or [""]
    lines = [f'{name} = {_vb_literal_part(chunks[0])}']
    lines.extend(f'{name} = {name} & {_vb_literal_part(chunk)}' for chunk in chunks[1:])
    return "\n".join(lines)


def make_paragraphs(record: dict[str, Any]) -> list[str]:
    # Bunlar kaynak künye verilerini anlatan şablonlardır. Bulgular uydurulmaz.
    title, context, query = record["title"], record["context"], record["query"]
    authors = record["authors"] or "Yazar bilgisi kaynak kaydında belirtilmemiş"
    year = record["year"] or "tarih bilgisi belirtilmemiş"
    venue = record["venue"] or "yayın yeri bilgisi kaynak kaydında belirtilmemiş"
    p1 = (f'“{title}” başlıklı yayın, {context} bağlamındaki kaynak taramasında listelenmiştir. '
          f'Kayıt, “{query}” arama ifadesi kullanılarak {record["provider"]} üzerinden bulunmuştur.')
    p2 = (f'Künye bilgilerine göre yazarlar: {authors}. Yayın yılı: {year}. '
          f'Yayın yeri veya dizin: {venue}. '
          + (f'Dijital nesne tanımlayıcısı: {record["doi"]}.' if record["doi"] else 'Bu kayıtta bir DOI tanımlayıcısı bulunmamaktadır.'))
    tokens = ", ".join(record.get("tokens", []))
    p3 = (f'Bu sayfa {record["category"]} kategorisinde keşif ve kaynak gösterme amacıyla hazırlanmış otomatik bir bibliyografik kayıttır. '
          + (f'Kategorinin tarama anahtarları arasında {tokens} yer alır. ' if tokens else '')
          + 'Çalışmanın yöntemi, örneklemi ve bulguları bu künye üzerinden çıkarılamaz; akademik kullanım öncesinde özgün yayını ve kaynak koşullarını inceleyin.')
    return [p1, p2, p3]


def build_asp(record: dict[str, Any]) -> str:
    p = make_paragraphs(record)
    variables = {
        "bbKind": "article", "bbTitle": record["title"], "bbCategory": record["category"],
        "bbCategoryId": record["category_id"], "bbYear": record["year"], "bbAuthors": record["authors"],
        "bbProvider": record["provider"], "bbSourceUrl": record["source_url"], "bbDoi": record["doi"],
        "bbVenue": record["venue"], "bbCreated": record["created_at"], "bbQuery": record["query"],
        "bbRecordId": record["id"], "bbP1": p[0], "bbP2": p[1], "bbP3": p[2],
        "bbDescription": f'{record["category"]} alanında {record["title"]} başlıklı kaynak kaydı.',
    }
    path = str(record.get("path", ""))
    if not re.fullmatch(r"/arsiv/[a-z0-9]+(?:-[a-z0-9]+)*/default\.asp", path):
        raise ValueError("Canonical için güvenli bir arşiv yolu gereklidir")
    canonical = "https://bilgibeceri.com" + path
    declaration = "Dim " + ", ".join(variables)
    assignments = "\n".join(vb_assignment(name, val) for name, val in variables.items())
    includes = "\n".join(f'<!--#include virtual="/{name}.asp" -->' for name in ("head", "header", "asie", "center", "shorts", "footer"))
    # Mevcut /head.asp dosyasını değiştirmeden, her yeni sayfanın kendi canonical
    # adresi bir HTTP Link yanıt başlığıyla belirtilir. HTTPS adresi sabittir.
    canonical_header = 'Response.AddHeader "Link", "<' + canonical + '>; rel=""canonical"""'
    return ('<%@ LANGUAGE="VBSCRIPT" CODEPAGE="65001" %><%\nOption Explicit\n'
            'Response.CodePage = 65001\nResponse.Charset = "utf-8"\n'
            + canonical_header + "\n" + declaration + "\n" + assignments + '\n%>\n' + includes + '\n')


def atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".bb-", suffix=".tmp", delete=False, newline="\n") as f:
            name = f.name
            json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        if name and os.path.exists(name):
            os.unlink(name)


def publish(record: dict[str, Any], root: Path) -> bool:
    archive = root / "arsiv"
    archive.mkdir(parents=True, exist_ok=True)
    record["id"] = hashlib.sha256(record["dedup_keys"][0].encode("utf-8")).hexdigest()[:12]
    folder = slugify(record["title"]) + "-" + record["id"]
    destination = archive / folder
    if destination.exists():
        return False
    record["path"] = "/arsiv/" + folder + "/default.asp"
    stage = Path(tempfile.mkdtemp(prefix=".bb-stage-", dir=archive))
    try:
        (stage / "default.asp").write_text(build_asp(record), encoding="utf-8", newline="\n")
        (stage / "meta.json").write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        try:
            os.rename(stage, destination)
        except FileExistsError:
            return False
        return True
    finally:
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)


def read_existing(root: Path) -> list[dict[str, Any]]:
    archive = root / "arsiv"
    if not archive.exists():
        return []
    records = []
    for meta in archive.glob("*/meta.json"):
        if meta.parent.name.startswith(".") or not (meta.parent / "default.asp").is_file():
            continue
        try:
            if meta.stat().st_size > 30_000:
                continue
            r = json.loads(meta.read_text(encoding="utf-8"))
            if not isinstance(r, dict) or not r.get("id") or not r.get("title"):
                continue
            if not re.fullmatch(r"[a-z0-9-]+", meta.parent.name):
                continue
            r["path"] = "/arsiv/" + meta.parent.name + "/default.asp"
            records.append(r)
        except (ValueError, OSError):
            LOGGER.warning("Geçersiz meta dosyası atlandı: %s", meta)
    return records


def catalog(root: Path, cfg: dict[str, Any], status: dict[str, Any] | None = None) -> int:
    records = read_existing(root)
    records.sort(key=lambda x: (str(x.get("created_at", "")), str(x.get("id", ""))), reverse=True)
    keys = ("id", "title", "authors", "year", "doi", "source_url", "provider", "provider_key", "venue",
            "work_type", "category_id", "category", "context", "query", "tokens", "created_at", "path")
    public = [{k: r.get(k, "" if k != "tokens" else []) for k in keys} for r in records]
    categories = []
    for c in cfg["categories"]:
        categories.append({"id": c["id"], "name": c["name"], "context": c.get("context", ""),
                           "count": sum(r.get("category_id") == c["id"] for r in public)})
    destination = root / "data" / "catalog.json"
    if status is None and destination.exists():
        try:
            previous = json.loads(destination.read_text(encoding="utf-8"))
            status = previous.get("sources", {}) if isinstance(previous, dict) else {}
        except (OSError, ValueError):
            status = {}
    atomic_json(destination, {
        "version": 1, "updated_at": now_iso(), "categories": categories,
        "sources": status or {}, "items": public,
    })
    return len(records)


def load_config(path: Path) -> tuple[dict[str, Any], Path]:
    cfg = json.loads(path.read_text(encoding="utf-8-sig"))
    root = Path(cfg.get("site_root", "../site"))
    if not root.is_absolute():
        root = path.parent / root
    root = root.resolve()
    if not (root / "SUNUCUYA-YUKLEMEYIN.txt").is_file() or not (root / "arsiv").is_dir() or not (root / "data").is_dir():
        raise ValueError("Üretim veri kökü doğrulanamadı: " + str(root))
    cfg["timeout_seconds"] = max(3, min(int(cfg.get("timeout_seconds", 18)), 90))
    cfg["pause_seconds"] = max(0, min(float(cfg.get("pause_seconds", 0.7)), 10))
    cfg["results_per_query_per_source"] = max(1, min(int(cfg.get("results_per_query_per_source", 2)), 20))
    cfg["max_new_per_run"] = max(1, min(int(cfg.get("max_new_per_run", 30)), 500))
    cfg["token_query_limit"] = max(0, min(int(cfg.get("token_query_limit", 0)), 5))
    if not isinstance(cfg.get("sources"), dict) or not any(cfg["sources"].get(k) is True for k in FETCHERS):
        raise ValueError("En az bir bilinen kaynak etkin olmalıdır")
    if not isinstance(cfg.get("categories"), list) or not cfg["categories"]:
        raise ValueError("En az bir kategori gereklidir")
    seen = set()
    for c in cfg["categories"]:
        if not isinstance(c, dict) or not SAFE_CATEGORY_ID.fullmatch(str(c.get("id", ""))) or not text(c.get("name")):
            raise ValueError("Her kategoride küçük harfli ASCII id ve ad bulunmalıdır")
        if c["id"] in seen:
            raise ValueError("Kategori id tekrarı: " + c["id"])
        seen.add(c["id"])
        if not isinstance(c.get("queries"), list) or not c["queries"]:
            raise ValueError("Kategoride en az bir sorgu gereklidir: " + c["id"])
        c["queries"] = [text(x, 240) for x in c["queries"] if text(x, 240)][:20]
        c["tokens"] = c.get("tokens", []) if isinstance(c.get("tokens", []), list) else []
    return cfg, root


@contextmanager
def single_run_lock():
    lock = HERE / ".generator.lock"
    try:
        fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise RuntimeError("Başka bir görev çalışıyor veya eski kilit kaldı: " + str(lock)) from exc
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(f"pid={os.getpid()} utc={now_iso()}\n")
        yield
    finally:
        try:
            lock.unlink(missing_ok=True)
        except OSError:
            LOGGER.exception("Kilit silinemedi")


def expanded_queries(category: dict[str, Any], cfg: dict[str, Any]) -> list[str]:
    """Açık sorguları kullan; sınırlandırılmış token çiftleriyle bunları genişlet."""
    queries = [text(q, 240) for q in category.get("queries", []) if text(q, 240)]
    tokens = [text(t, 80) for t in category.get("tokens", []) if text(t, 80)]
    limit = max(0, min(int(cfg.get("token_query_limit", 0)), 5))
    for i in range(min(limit, len(tokens))):
        candidate = tokens[i] + ((" " + tokens[(i + 1) % len(tokens)]) if len(tokens) > 1 else "")
        if candidate.casefold() not in {q.casefold() for q in queries}:
            queries.append(candidate)
    return queries


def refresh_pages(root: Path) -> int:
    """Meta veriye dayanarak mevcut sayfa değişkenlerini atomik biçimde yenile."""
    done = 0
    for record in read_existing(root):
        target = root / record["path"].lstrip("/")
        temp = None
        try:
            content = build_asp(record)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=target.parent,
                                             prefix=".bb-page-", suffix=".tmp", delete=False, newline="\n") as f:
                temp = f.name
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp, target)
            done += 1
        except (KeyError, TypeError, OSError) as exc:
            LOGGER.warning("Sayfa yenilenemedi: %s (%s)", target, type(exc).__name__)
        finally:
            if temp and os.path.exists(temp):
                os.unlink(temp)
    return done


def run(cfg: dict[str, Any], root: Path, dry_run: bool = False) -> dict[str, Any]:
    existing = read_existing(root)
    seen: set[str] = set()
    for r in existing:
        seen.update(r.get("dedup_keys") or fingerprint_keys(r))
    status = {PROVIDER_NAMES[k]: {"state": "pending" if enabled else "disabled"}
              for k, enabled in cfg.get("sources", {}).items() if k in PROVIDER_NAMES}
    queues: list[list[dict[str, Any]]] = []
    blocked: set[str] = set()
    for cat in cfg["categories"]:
        queue: list[dict[str, Any]] = []
        for query in expanded_queries(cat, cfg):
            for key in FETCHERS:
                if not cfg.get("sources", {}).get(key, False) or key in blocked:
                    continue
                try:
                    raw_items = FETCHERS[key](query, cfg)
                    status[PROVIDER_NAMES[key]] = {"state": "ok"}
                    LOGGER.info("%s / %s / %s: %d sonuç", PROVIDER_NAMES[key], cat["id"], query, len(raw_items))
                    for raw in raw_items:
                        r = normalize(raw, cat, query, key)
                        if r:
                            queue.append(r)
                except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError, TypeError, AttributeError, IndexError, ET.ParseError) as exc:
                    code = getattr(exc, "code", None)
                    # URL veya erişim anahtarı değil, yalnızca hata sınıfı/kodu yazılır.
                    reason = f"HTTP {code}" if code else type(exc).__name__
                    LOGGER.warning("%s: %s; bu çalışmada diğer kaynaklar sürer", PROVIDER_NAMES[key], reason)
                    # Bir sağlayıcı önce başarıyla yanıt vermişse, sonraki kota/ağ
                    # hatası bütün çalışmayı başarısız göstermemelidir.
                    previous_state = status[PROVIDER_NAMES[key]].get("state")
                    state = "partial" if previous_state == "ok" else "error"
                    status[PROVIDER_NAMES[key]] = {"state": state, "message": reason}
                    blocked.add(key)
                if cfg["pause_seconds"]:
                    time.sleep(cfg["pause_seconds"])
        queues.append(queue)
    # Kategoriler arasında adil döngü. Aynı kaydın farklı kaynaklardaki DOI/başlıkları birleştirilir.
    created = 0
    while any(queues) and created < cfg["max_new_per_run"]:
        for queue in queues:
            if not queue or created >= cfg["max_new_per_run"]:
                continue
            r = queue.pop(0)
            if any(k in seen for k in r["dedup_keys"]):
                continue
            if dry_run:
                ok = True
            else:
                ok = publish(r, root)
            if ok:
                created += 1
                seen.update(r["dedup_keys"])
                LOGGER.info("%s: %s", "ÖN İZLEME" if dry_run else "YAYINLANDI", r["title"][:120])
    count = len(existing) if dry_run else catalog(root, cfg, status)
    return {"new": created, "total": count, "sources": status}


def main() -> int:
    parser = argparse.ArgumentParser(description="Bilgi Beceri akademik arşiv üreticisi")
    parser.add_argument("action", choices=("run", "rebuild", "refresh", "check"), help="run: kaynakları tara; rebuild: katalog oluştur; refresh: sayfaları yenile; check: ayarları doğrula")
    parser.add_argument("--config", default=str(HERE / "settings.json"))
    parser.add_argument("--dry-run", action="store_true", help="Kaynakları ara, dosya üretme")
    args = parser.parse_args()
    logs = HERE / "logs"
    logs.mkdir(exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=[
        logging.StreamHandler(), logging.FileHandler(logs / "generator.log", encoding="utf-8")])
    try:
        cfg, root = load_config(Path(args.config).resolve())
        if args.action == "check":
            print("OK - site kökü:", root)
            print("Kategoriler:", ", ".join(c["name"] for c in cfg["categories"]))
            print("Kaynaklar:", ", ".join(PROVIDER_NAMES[k] for k in FETCHERS if cfg.get("sources", {}).get(k)))
            return 0
        with single_run_lock():
            if args.action == "rebuild":
                result = {"total": catalog(root, cfg), "new": 0}
            elif args.action == "refresh":
                result = {"refreshed": refresh_pages(root), "total": catalog(root, cfg)}
            else:
                result = run(cfg, root, args.dry_run)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if args.action == "run" and not any(s.get("state") in ("ok", "partial") for s in result.get("sources", {}).values()):
            LOGGER.error("Hiçbir kaynak erişilebilir değildi; mevcut yayınlar korunarak hata kodu döndürülüyor.")
            return 2
        return 0
    except (ValueError, OSError, RuntimeError, json.JSONDecodeError) as exc:
        LOGGER.error("Görev tamamlanamadı: %s", exc)
        return 1

if __name__ == "__main__":
    sys.exit(main())
