"""
按章节类型分库：BM25 + FAISS。检索必须带类型，不能搜整份。
建完后把各章 FAISS 写到 data/tender_index/<pdf文件名>/。
"""

import json
import shutil
from pathlib import Path

from langchain_community.vectorstores import FAISS
from langchain_huggingface import HuggingFaceEmbeddings
from rank_bm25 import BM25Okapi

from tender.sections import TYPE_LABEL, section_of_page

INDEX_VERSION = 3  # 3 = 块带字号/页眉标记（2 只有块级 bbox；旧缓存必须重建）

DEFAULT_INDEX_ROOT = Path(__file__).resolve().parent.parent / "data" / "tender_index"
DEFAULT_EMBED_MODEL = "BAAI/bge-small-zh-v1.5"

# 进程内缓存，避免反复加载 BGE
_EMBEDDINGS = None


def get_embeddings(model=DEFAULT_EMBED_MODEL):
    """本地 BGE，和旧主库同一套。"""
    global _EMBEDDINGS
    if _EMBEDDINGS is None:
        _EMBEDDINGS = HuggingFaceEmbeddings(
            model_name=model,
            model_kwargs={"device": "cpu"},
            encode_kwargs={"normalize_embeddings": True},
        )
    return _EMBEDDINGS


def _tokenize(text: str) -> list[str]:
    """中文按字切，供 BM25 用。"""
    return [ch for ch in text if not ch.isspace()]


def pick_block(query: str, blocks: list[dict], page_height: float = 842.0) -> dict | None:
    """在段内挑和 query 字面重合最多的那一块——比整段并集窄得多。

    同分时挑更矮的块（更可能是一行条款而不是一段正文）。返回
    {"bbox": [...], "kind": "block"|"page"}；没有块返回 None。
    """
    if not blocks:
        return None
    q = set(_tokenize(query))
    best, best_score, best_h = None, -1, float("inf")
    for b in blocks:
        score = len(q & set(_tokenize(b.get("text") or "")))
        h = b["bbox"][3] - b["bbox"][1]
        if score > best_score or (score == best_score and h < best_h):
            best, best_score, best_h = b, score, h
    kind = "page" if best_h > page_height * 0.6 else "block"
    return {"bbox": list(best["bbox"]), "kind": kind}


def chunk_pages(pages: list[dict], sections: list[dict], target_len: int = 380) -> list[dict]:
    """章内按块拼段，不跨页；保留每个块的 bbox，供检索时挑最准的那一块。"""
    chunks = []
    buf = []
    buf_len = 0

    def flush(page, sec):
        nonlocal buf, buf_len
        if not buf or sec is None:
            buf, buf_len = [], 0
            return
        text = "\n".join(b["text"] for b in buf)
        box = list(buf[0]["bbox"])
        for b in buf[1:]:
            x0, y0, x1, y1 = b["bbox"]
            box = [min(box[0], x0), min(box[1], y0), max(box[2], x1), max(box[3], y1)]
        chunks.append({
            "text": text,
            "page": page["page"],
            "page_height": page.get("height") or 842.0,
            "source": page.get("source"),
            "section_type": sec["type"],
            "section_title": sec["title"],
            "bbox": box,  # 段的并集，作兜底
            # 块的版面线索跟着进索引：切条款要用字号，去噪要用页眉标。
            # 这里只搬运字段，pick_block 的选块逻辑不动。
            "blocks": [{
                "text": b["text"],
                "bbox": list(b["bbox"]),
                "max_size": b.get("max_size"),
                "is_header": bool(b.get("is_header")),
            } for b in buf],
        })
        buf, buf_len = [], 0

    for page in pages:
        sec = section_of_page(sections, page["page"])
        for block in page.get("blocks", []):
            t = block["text"].strip()
            if not t:
                continue
            if buf and buf_len + len(t) > target_len:
                flush(page, sec)
            buf.append(block)
            buf_len += len(t)
        flush(page, sec)
    return chunks


class SectionIndex:
    """某一章类型的 BM25 + 向量库。"""

    def __init__(self, chunks: list[dict]):
        self.chunks = chunks
        self.bm25 = None
        self.vs = None
        if chunks:
            self.bm25 = BM25Okapi([_tokenize(c["text"]) for c in chunks])
            metadatas = [{
                "page": c["page"],
                "page_height": c.get("page_height") or 842.0,
                "section_type": c["section_type"],
                "section_title": c["section_title"],
                "bbox": ",".join(str(x) for x in c["bbox"]),
                "blocks": json.dumps(c.get("blocks") or [], ensure_ascii=False),
                "source": c.get("source"),
            } for c in chunks]
            self.vs = FAISS.from_texts(
                [c["text"] for c in chunks],
                get_embeddings(),
                metadatas=metadatas,
            )

    @classmethod
    def load(cls, folder: Path) -> "SectionIndex":
        """从已保存的 FAISS 恢复这一章。BM25 用库里的原文重建，不再重新向量化。"""
        vs = FAISS.load_local(
            str(folder),
            get_embeddings(),
            allow_dangerous_deserialization=True,
        )
        chunks = []
        for idx in sorted(vs.index_to_docstore_id):
            doc = vs.docstore.search(vs.index_to_docstore_id[idx])
            raw = str(doc.metadata.get("bbox") or "")
            bbox = [float(x) for x in raw.split(",") if x]
            chunks.append({
                "text": doc.page_content,
                "page": doc.metadata.get("page"),
                "page_height": doc.metadata.get("page_height") or 842.0,
                "source": doc.metadata.get("source"),
                "section_type": doc.metadata.get("section_type"),
                "section_title": doc.metadata.get("section_title"),
                "bbox": bbox,
                "blocks": json.loads(doc.metadata.get("blocks") or "[]"),
            })
        self = cls.__new__(cls)
        self.chunks = chunks
        self.vs = vs
        self.bm25 = BM25Okapi([_tokenize(c["text"]) for c in chunks]) if chunks else None
        return self

    def _hit(self, query: str, chunk: dict) -> dict:
        """把段变成检索命中：bbox 换成段内最匹配的块，块级框才算「能定位」。"""
        precise = pick_block(query, chunk.get("blocks") or [], chunk.get("page_height") or 842.0)
        return {
            "text": chunk["text"],
            "page": chunk["page"],
            "source": chunk.get("source"),
            "section_type": chunk.get("section_type"),
            "section_title": chunk.get("section_title"),
            "bbox": precise["bbox"] if precise else chunk["bbox"],
            "bbox_union": chunk["bbox"],
            "bbox_kind": precise["kind"] if precise else "chunk",
        }

    def search(self, query: str, k: int = 4) -> list[dict]:
        """先 BM25 再向量，合并去重。命中里带块级 bbox。"""
        if not self.chunks:
            return []
        scored = self.bm25.get_scores(_tokenize(query))
        bm25_idx = sorted(range(len(scored)), key=lambda i: scored[i], reverse=True)[:k]
        vec_hits = self.vs.similarity_search(query, k=k)
        picked = []
        seen = set()
        for i in bm25_idx:
            c = self.chunks[i]
            key = (c["page"], c["text"][:40])
            if key in seen:
                continue
            seen.add(key)
            picked.append(self._hit(query, c))
        for doc in vec_hits:
            key = (doc.metadata.get("page"), doc.page_content[:40])
            if key in seen:
                continue
            seen.add(key)
            bbox = []
            raw = doc.metadata.get("bbox") or ""
            if raw:
                bbox = [float(x) for x in raw.split(",")]
            picked.append(self._hit(query, {
                "text": doc.page_content,
                "page": doc.metadata.get("page"),
                "page_height": doc.metadata.get("page_height") or 842.0,
                "source": doc.metadata.get("source"),
                "section_type": doc.metadata.get("section_type"),
                "section_title": doc.metadata.get("section_title"),
                "bbox": bbox,
                "blocks": json.loads(doc.metadata.get("blocks") or "[]"),
            }))
        return picked[: k * 2]


class TenderIndex:
    """按章类型分库的总索引。"""

    def __init__(self, stores: dict[str, SectionIndex], sections: list[dict], pdf_path: str):
        self.stores = stores
        self.sections = sections
        self.pdf_path = pdf_path

    def search(self, query: str, section_types: list[str], k: int = 4) -> list[dict]:
        """只在指定章类型里搜。"""
        hits = []
        for typ in section_types:
            store = self.stores.get(typ)
            if store is None:
                continue
            hits.extend(store.search(query, k=k))
        hits.sort(key=lambda h: h.get("page") or 0)
        return hits


def save_tender_index(index: TenderIndex) -> Path:
    """把各章向量库写到 data/tender_index/<pdf文件名>/<章类型>/。同一份 PDF 再跑会覆盖该目录。"""
    root = DEFAULT_INDEX_ROOT / Path(index.pdf_path).stem
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    saved = []
    for typ, store in index.stores.items():
        if store.vs is None:
            continue
        folder = root / typ
        folder.mkdir(parents=True, exist_ok=True)
        store.vs.save_local(str(folder))
        saved.append(typ)
    manifest = {
        "index_version": INDEX_VERSION,
        "pdf_path": index.pdf_path,
        "sections": index.sections,
        "types": saved,
    }
    (root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return root


def load_tender_index(pdf_path) -> TenderIndex:
    """按 PDF 文件名载入 data/tender_index/<文件名>/。没有这份库就报错。"""
    root = DEFAULT_INDEX_ROOT / Path(pdf_path).stem
    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"没有已保存的向量库: {root}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    stores = {}
    for typ in manifest.get("types") or []:
        stores[typ] = SectionIndex.load(root / typ)
    return TenderIndex(stores, manifest.get("sections") or [], str(pdf_path))


def build_tender_index(pages: list[dict], sections: list[dict], pdf_path: str, on_chapter=None) -> TenderIndex:
    """切块并按类型建库，并把向量库写到 data/tender_index。"""
    chunks = chunk_pages(pages, sections)
    grouped = {}
    for c in chunks:
        grouped.setdefault(c["section_type"], []).append(c)
    print(f"切块 {len(chunks)} 段，开始按章建向量索引", flush=True)
    stores = {}
    for typ, items in grouped.items():
        label = TYPE_LABEL.get(typ, typ)
        print(f"  建库 {label}  {len(items)} 段", flush=True)
        if on_chapter is not None:
            on_chapter(label, len(items))
        stores[typ] = SectionIndex(items)
    index = TenderIndex(stores, sections, str(pdf_path))
    folder = save_tender_index(index)
    print(f"索引建完，已写入 {folder}", flush=True)
    return index
