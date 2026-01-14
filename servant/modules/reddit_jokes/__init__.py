from __future__ import annotations

import json
import re
from pathlib import Path

import numpy as np
from numpy.typing import NDArray

from servant.defs import ToolDef
from typed_json import JSONDict, coerce_float

_script_dir = Path(__file__).parent

with (_script_dir / "reddit_jokes.json").open("r", encoding="utf-8") as handle:
    reddit_jokes: list[JSONDict] = json.load(handle)

scores_list = [coerce_float(j.get("score"), 0.0) + 1.0 for j in reddit_jokes]
scores: NDArray[np.float64] = np.array(scores_list, dtype=float)
scores = scores**0.5
scores = scores / scores.sum()

with (_script_dir / "bad_words.txt").open("rt", encoding="utf-8") as handle:
    bad_words_list = handle.read().splitlines()
bad_words: set[str] = {w.strip().lower() for w in bad_words_list if w.strip()}

BAD_WORD_RE = re.compile(r"\b(" + "|".join(re.escape(w) for w in bad_words) + r")\b")


SEEN_JOKE_COUNT = 128  # Before we allow repeats
seen_jokes: list[int] = []
seen_jokes_set: set[int] = set()


async def get_joke() -> JSONDict:
    global seen_jokes, seen_jokes_set, reddit_jokes, scores
    if len(seen_jokes) >= SEEN_JOKE_COUNT:
        j = seen_jokes.pop(0)
        seen_jokes_set.remove(j)
    else:
        while True:
            j = int(np.random.choice(np.arange(len(scores)), p=scores))
            opening = reddit_jokes[j]["title"]
            punchline = reddit_jokes[j]["body"]
            # if BAD_WORD_RE.search(opening) or BAD_WORD_RE.search(punchline):
            #     continue
            if j not in seen_jokes_set:
                break
    seen_jokes.append(j)
    seen_jokes_set.add(j)
    return {
        "opening": reddit_jokes[j]["title"],
        "punchline": reddit_jokes[j]["body"],
        "reddit_score": reddit_jokes[j]["score"],
    }


get_joke_tool: ToolDef = ToolDef(
    name="get_joke",
    schema={"name": "get_joke", "description": "Get a random joke."},
    function=lambda ctx, obj: get_joke(),
)
