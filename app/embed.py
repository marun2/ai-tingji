"""本地向量模型 + 向量索引（会议内容不出服务器）。

- 模型：bge-base-zh-v1.5 的 ONNX 量化版（约 100MB，768 维），第一次启动时从 hf-mirror / huggingface 下载到 DATA_DIR/models
  下载失败不影响使用：提问和搜索退回只用关键词，管理页能看到状态并重试
- 分词：BERT WordPiece（纯 Python 实现，读 tokenizer.json 的词表），不依赖 transformers / tokenizers
- 索引：每个文本段一个向量（float16），存在 DATA_DIR/vectors.npz；不进备份，丢了会从文字重新生成
- 后台线程：发现还没向量的文本段就补上（导入后自动开始）
"""
import json
import os
import pathlib
import re
import threading
import time
import unicodedata
import urllib.request

import numpy as np

from . import db

MODEL_REPO = "Xenova/bge-base-zh-v1.5"
MODEL_FILES = {"model.onnx": "onnx/model_quantized.onnx", "tokenizer.json": "tokenizer.json"}
MIRRORS = ["https://hf-mirror.com", "https://huggingface.co"]
MODEL_DIR = db.DATA_DIR / "models" / "bge-base-zh-v1.5-q"
INDEX_PATH = db.DATA_DIR / "vectors.npz"
DIM = 768
MAX_LEN = 512
QUERY_PREFIX = "为这个句子生成表示以用于检索相关文章："
BATCH = 8

_state = {"status": "idle", "detail": "", "indexed": 0, "total": 0, "updated": ""}
_lock = threading.RLock()
_session = None
_tok = None
_ids = np.zeros(0, dtype=np.int64)        # 文本段 id
_mids = np.zeros(0, dtype=np.int64)       # 所属会议 id
_vecs = np.zeros((0, DIM), dtype=np.float16)
_wake = threading.Event()


def state() -> dict:
    with _lock:
        return {**_state, "indexed": int(len(_ids))}


def _set(**kw):
    with _lock:
        _state.update(kw, updated=time.strftime("%Y-%m-%d %H:%M:%S"))


# ---------------------------------------------------------------- 分词（BERT WordPiece）
class WordPiece:
    def __init__(self, tokenizer_json: pathlib.Path):
        d = json.loads(tokenizer_json.read_text(encoding="utf-8"))
        self.vocab: dict[str, int] = d["model"]["vocab"]
        self.unk = self.vocab.get("[UNK]", 100)
        self.cls = self.vocab.get("[CLS]", 101)
        self.sep = self.vocab.get("[SEP]", 102)
        self.lower = True

    @staticmethod
    def _is_cjk(cp: int) -> bool:
        return (0x4E00 <= cp <= 0x9FFF or 0x3400 <= cp <= 0x4DBF or 0x20000 <= cp <= 0x2A6DF or 0xF900 <= cp <= 0xFAFF
                or 0x2F800 <= cp <= 0x2FA1F)

    def _basic(self, text: str) -> list[str]:
        out, buf = [], []
        if self.lower:
            text = text.lower()
            text = "".join(c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn")
        for ch in text:
            cp = ord(ch)
            cat = unicodedata.category(ch)
            if ch.isspace() or cat.startswith("C"):
                if buf:
                    out.append("".join(buf)); buf = []
            elif self._is_cjk(cp) or cat.startswith("P") or (33 <= cp <= 47 or 58 <= cp <= 64 or 91 <= cp <= 96 or 123 <= cp <= 126):
                if buf:
                    out.append("".join(buf)); buf = []
                out.append(ch)
            else:
                buf.append(ch)
        if buf:
            out.append("".join(buf))
        return out

    def _wordpiece(self, word: str) -> list[int]:
        if len(word) > 100:
            return [self.unk]
        ids, start = [], 0
        while start < len(word):
            end, cur = len(word), None
            while start < end:
                sub = word[start:end] if start == 0 else "##" + word[start:end]
                if sub in self.vocab:
                    cur = self.vocab[sub]
                    break
                end -= 1
            if cur is None:
                return [self.unk]
            ids.append(cur)
            start = end
        return ids

    def encode(self, text: str, max_len: int = MAX_LEN) -> list[int]:
        ids = []
        for w in self._basic(text):
            ids.extend(self._wordpiece(w))
            if len(ids) >= max_len - 2:
                break
        return [self.cls] + ids[: max_len - 2] + [self.sep]


# ---------------------------------------------------------------- 模型
def _download():
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    for local, remote in MODEL_FILES.items():
        dst = MODEL_DIR / local
        if dst.exists() and dst.stat().st_size > 1000:
            continue
        last = None
        for base in MIRRORS:
            url = f"{base}/{MODEL_REPO}/resolve/main/{remote}"
            _set(status="downloading", detail=f"下载 {local}（{base.split('//')[1]}）")
            try:
                tmp = dst.with_suffix(".part")
                with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "ai-tingji"}), timeout=60) as r, open(tmp, "wb") as f:
                    while True:
                        b = r.read(1 << 20)
                        if not b:
                            break
                        f.write(b)
                tmp.replace(dst)
                last = None
                break
            except Exception as e:  # noqa: BLE001
                last = f"{base}: {e}"
        if last:
            raise RuntimeError(f"向量模型下载失败：{last}")


def _load():
    global _session, _tok
    import onnxruntime as ort
    opts = ort.SessionOptions()
    opts.intra_op_num_threads = int(os.environ.get("EMBED_THREADS", "2"))
    opts.inter_op_num_threads = 1
    _session = ort.InferenceSession(str(MODEL_DIR / "model.onnx"), opts, providers=["CPUExecutionProvider"])
    _tok = WordPiece(MODEL_DIR / "tokenizer.json")


def ready() -> bool:
    return _session is not None


def embed(texts: list[str], query: bool = False) -> np.ndarray:
    if not ready():
        raise RuntimeError("向量模型还没准备好")
    seqs = [_tok.encode((QUERY_PREFIX + t) if query else t) for t in texts]
    n = max(len(s) for s in seqs)
    ids = np.zeros((len(seqs), n), dtype=np.int64)
    mask = np.zeros((len(seqs), n), dtype=np.int64)
    for i, s in enumerate(seqs):
        ids[i, : len(s)] = s
        mask[i, : len(s)] = 1
    feeds = {"input_ids": ids, "attention_mask": mask}
    names = {i.name for i in _session.get_inputs()}
    if "token_type_ids" in names:
        feeds["token_type_ids"] = np.zeros_like(ids)
    out = _session.run(None, feeds)[0]
    v = out[:, 0, :] if out.ndim == 3 else out          # bge 用 [CLS] 向量
    v = v / np.maximum(np.linalg.norm(v, axis=1, keepdims=True), 1e-9)
    return v.astype(np.float32)


# ---------------------------------------------------------------- 索引
def _load_index():
    global _ids, _mids, _vecs
    if INDEX_PATH.exists():
        try:
            z = np.load(INDEX_PATH)
            if z["vecs"].shape[1] == DIM:
                _ids, _mids, _vecs = z["ids"], z["mids"], z["vecs"]
        except Exception as e:  # noqa: BLE001
            print("向量索引读取失败，重新生成：", e, flush=True)


def _save_index():
    tmp = INDEX_PATH.with_suffix(".tmp.npz")
    with _lock:
        np.savez(tmp, ids=_ids, mids=_mids, vecs=_vecs)
    tmp.replace(INDEX_PATH)


def forget_meetings(mids: list[int]):
    """会议内容变了 / 删除了：去掉旧向量（后台会按新文本重新生成）"""
    global _ids, _mids, _vecs
    if not mids:
        return
    with _lock:
        keep = ~np.isin(_mids, np.array(mids, dtype=np.int64))
        _ids, _mids, _vecs = _ids[keep], _mids[keep], _vecs[keep]
    _wake.set()


def search(qvec: np.ndarray, allowed_mids: set[int], top: int = 40) -> list[tuple[int, float]]:
    """在允许的会议里找最像的文本段：返回 [(文本段 id, 相似度)]"""
    with _lock:
        if not len(_ids):
            return []
        mask = np.isin(_mids, np.fromiter(allowed_mids, dtype=np.int64)) if allowed_mids is not None else np.ones(len(_ids), bool)
        if not mask.any():
            return []
        idx = np.nonzero(mask)[0]
        sims = _vecs[idx].astype(np.float32) @ qvec.astype(np.float32)
        order = np.argsort(-sims)[:top]
        return [(int(_ids[idx[i]]), float(sims[i])) for i in order]


def _worker():
    global _ids, _mids, _vecs
    try:
        _set(status="loading", detail="准备向量模型")
        _download()
        _load()
        _load_index()
    except Exception as e:  # noqa: BLE001
        _set(status="failed", detail=str(e)[:300])
        return
    _set(status="ready", detail="")
    while True:
        try:
            have = set(_ids.tolist())
            todo = [r for r in db.q("SELECT id, meeting_id, text FROM chunk ORDER BY id") if r["id"] not in have]
            _set(total=len(have) + len(todo))
            if todo:
                _set(status="indexing", detail=f"还差 {len(todo)} 段")
                since = 0
                for i in range(0, len(todo), BATCH):
                    part = todo[i:i + BATCH]
                    v = embed([p["text"] for p in part]).astype(np.float16)
                    with _lock:
                        _ids = np.concatenate([_ids, np.array([p["id"] for p in part], dtype=np.int64)])
                        _mids = np.concatenate([_mids, np.array([p["meeting_id"] for p in part], dtype=np.int64)])
                        _vecs = np.concatenate([_vecs, v])
                    since += len(part)
                    if since >= 400:
                        _save_index(); since = 0
                        _set(detail=f"还差 {len(todo) - i - len(part)} 段")
                _save_index()
            # 清理已经不存在的文本段
            alive = {r["id"] for r in db.q("SELECT id FROM chunk")}
            with _lock:
                keep = np.isin(_ids, np.fromiter(alive, dtype=np.int64)) if len(_ids) else np.zeros(0, bool)
                if len(_ids) and not keep.all():
                    _ids, _mids, _vecs = _ids[keep], _mids[keep], _vecs[keep]
                    _save_index()
            _set(status="ready", detail="", total=len(alive))
        except Exception as e:  # noqa: BLE001
            _set(status="error", detail=f"生成向量出错：{str(e)[:200]}（稍后自动重试）")
        _wake.wait(timeout=600)
        _wake.clear()


_started = False


def start():
    """应用启动时调用；EMBED=off 时不启动（冒烟测试用）"""
    global _started
    if _started or os.environ.get("EMBED", "on") == "off":
        return
    _started = True
    threading.Thread(target=_worker, daemon=True).start()


def kick():
    _wake.set()


def retry():
    global _started
    if _state["status"] == "failed":
        _started = False
        start()
