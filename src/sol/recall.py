"""Find useful evidence in past sessions.

Use a deterministic BM25 index because function names and error messages are
often best matched lexically. Tokenize Hangul as syllable bigrams so Korean
queries remain searchable without an external analyzer.
"""

import json
import math
import os
import re
from collections import Counter

from .determinism import fold_dev_terms

K1 = 1.5
B = 0.75

MAX_RESULTS = 8
SNIPPET_CHARS = 240

_WORD = re.compile(r"[A-Za-z0-9_]+")
_HANGUL = re.compile(r"[가-힣]+")


def tokenize(text: str) -> list[str]:
    """Split English and digits into words and Hangul into syllable bigrams.

    Bigrams tolerate common particle changes without requiring a morphology
    dependency. Fold domain synonyms before indexing so equivalent terms match.
    """
    lowered = fold_dev_terms(text).lower()
    tokens = _WORD.findall(lowered)
    for run in _HANGUL.findall(fold_dev_terms(text)):
        if len(run) == 1:
            tokens.append(run)
            continue
        tokens += [run[i:i + 2] for i in range(len(run) - 1)]
    return tokens


class Index:
    """BM25 index whose documents are ``(id, body, metadata)`` triples."""

    def __init__(self) -> None:
        self.docs: list[dict] = []
        self.frequencies: list[Counter] = []
        self.lengths: list[int] = []
        self.document_frequency: Counter = Counter()

    def add(self, doc_id: str, text: str, meta: dict | None = None) -> None:
        tokens = tokenize(text)
        if not tokens:
            return
        counts = Counter(tokens)
        self.docs.append({"id": doc_id, "text": text, "meta": meta or {}})
        self.frequencies.append(counts)
        self.lengths.append(len(tokens))
        for term in counts:
            self.document_frequency[term] += 1

    def __len__(self) -> int:
        return len(self.docs)

    def search(self, query: str, limit: int = MAX_RESULTS) -> list[dict]:
        if not self.docs:
            return []
        terms = tokenize(query)
        if not terms:
            return []

        total = len(self.docs)
        average = sum(self.lengths) / total
        scored = []
        for index, counts in enumerate(self.frequencies):
            score = 0.0
            length = self.lengths[index]
            for term in terms:
                frequency = counts.get(term, 0)
                if not frequency:
                    continue
                appearances = self.document_frequency[term]
                idf = math.log(1 + (total - appearances + 0.5) /
                               (appearances + 0.5))
                denominator = frequency + K1 * (1 - B + B * length / average)
                score += idf * frequency * (K1 + 1) / denominator
            if score > 0:
                scored.append((score, index))

        scored.sort(key=lambda pair: (-pair[0], pair[1]))
        if not scored:
            return []

        # Treat low scores relative to the best match as noise. Common-word
        # overlap alone should not be presented as useful evidence.
        floor = scored[0][0] * 0.25
        results = []
        for score, index in scored[:limit]:
            if score < floor:
                break
            doc = self.docs[index]
            results.append({"id": doc["id"], "score": round(score, 3),
                            "snippet": doc["text"][:SNIPPET_CHARS],
                            **doc["meta"]})
        return results


def index_sessions(root: str, cwd: str = "") -> Index:
    """Index stored session JSONL using user messages and final answers only.

    Excluding tool results prevents file contents from dominating conversation
    search.
    """
    index = Index()
    if not os.path.isdir(root):
        return index

    for name in sorted(os.listdir(root)):
        if not name.endswith(".jsonl"):
            continue
        path = os.path.join(root, name)
        session_cwd = ""
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        break
                    if record.get("type") == "header":
                        session_cwd = record.get("cwd", "")
                        if cwd and session_cwd != cwd:
                            break
                        continue
                    if record.get("type") != "message":
                        continue
                    message = record.get("message") or {}
                    if message.get("role") not in ("user", "assistant"):
                        continue
                    content = str(message.get("content") or "").strip()
                    if len(content) < 12:
                        continue
                    index.add(f"{name}:{record['id']}", content,
                              {"session": name, "role": message["role"]})
        except OSError:
            continue
    return index


def recall_tool(root: str, cwd: str = ""):
    """Build a tool for searching past sessions.

    Build the index at call time and keep it out of the prefix so new sessions do
    not invalidate the cache.
    """
    from .tools import Tool

    def run(args: dict) -> dict:
        index = index_sessions(root, cwd)
        if not len(index):
            return {"hits": [], "note": "검색할 과거 세션이 없습니다"}
        hits = index.search(args["query"], args.get("limit", MAX_RESULTS))
        if not hits:
            return {"hits": [],
                    "note": "일치하는 기록이 없습니다. 더 드문 단어로 다시 시도하십시오"}
        return {"hits": hits}

    return Tool(
        name="recall",
        description=("과거 작업 기록에서 관련 내용을 찾는다. "
                     "'전에 어떻게 했더라' 같은 상황에 쓴다. "
                     "현재 코드를 찾을 때는 grep을 쓴다."),
        parameters={"type": "object",
                    "properties": {"query": {"type": "string"},
                                   "limit": {"type": "integer"}},
                    "required": ["query"]},
        run=run)
