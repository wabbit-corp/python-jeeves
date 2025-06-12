from __future__ import annotations
import json
import re
import numpy as np
from servant.defs import ToolDef, JSONDict
from pathlib import Path

_script_dir = Path(__file__).parent

reddit_jokes = json.loads(open(_script_dir / "reddit_jokes.json").read())
scores = [j["score"] + 1 for j in reddit_jokes]
scores = np.array(scores)
scores = scores ** (1 / 2)
scores = scores / scores.sum()

bad_words = (
    open(_script_dir / "bad_words.txt", "rt", encoding="utf-8").read().splitlines()
)
bad_words = set([w.strip().lower() for w in bad_words if w.strip()])

BAD_WORD_RE = re.compile(r"\b(" + "|".join(re.escape(w) for w in bad_words) + r")\b")


SEEN_JOKE_COUNT = 128  # Before we allow repeats
seen_jokes = []
seen_jokes_set = set()


async def get_joke() -> JSONDict:
    global seen_jokes, seen_jokes_set, reddit_jokes, scores
    if len(seen_jokes) >= SEEN_JOKE_COUNT:
        j = seen_jokes.pop(0)
        seen_jokes_set.remove(j)
    else:
        while True:
            j = np.random.choice(np.arange(len(scores)), p=scores)
            opening = reddit_jokes[j]["title"]
            punchline = reddit_jokes[j]["body"]
            if BAD_WORD_RE.search(opening) or BAD_WORD_RE.search(punchline):
                continue
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
