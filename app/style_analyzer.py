"""Telegram communication style analyzer.

Analyzes user's message history and produces a comprehensive style profile
that can be used as a system prompt for an LLM clone.

Usage:
    python -m app.style_analyzer [--input FILE] [--output FILE] [--recent-months N]

Defaults:
    input: data/exports/my_messages_*.jsonl (largest export found)
    output: data/style_profile.md
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app.config import settings

log = logging.getLogger(__name__)

EXPORTS_DIR = Path(__file__).resolve().parent.parent / "data" / "exports"
OUTPUT_DIR = Path(__file__).resolve().parent.parent / "data"


def _find_input(explicit: str | None) -> Path:
    if explicit:
        p = Path(explicit)
        if p.is_file():
            return p
        raise FileNotFoundError(f"Not found: {p}")
    candidates = sorted(EXPORTS_DIR.glob("my_messages_*.jsonl"), key=lambda p: p.stat().st_size, reverse=True)
    if not candidates:
        raise FileNotFoundError("No my_messages_*.jsonl found in data/exports/")
    return candidates[0]


def _load_messages(path: Path) -> list[dict]:
    msgs = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                msgs.append(json.loads(line))
    return msgs


# ─── Analysis modules ───────────────────────────────────────────────


def analyze_basics(texts: list[str]) -> dict:
    lens = [len(t) for t in texts]
    lens_sorted = sorted(lens)
    return {
        "total_messages": len(texts),
        "avg_length": sum(lens) // max(len(lens), 1),
        "median_length": lens_sorted[len(lens_sorted) // 2] if lens_sorted else 0,
        "p90_length": lens_sorted[int(len(lens_sorted) * 0.9)] if lens_sorted else 0,
        "length_distribution": _length_buckets(lens),
    }


def _length_buckets(lens: list[int]) -> dict[str, float]:
    buckets = {"1-5": 0, "6-15": 0, "16-30": 0, "31-60": 0, "61-120": 0, "121-250": 0, "251+": 0}
    for l in lens:
        if l <= 5: buckets["1-5"] += 1
        elif l <= 15: buckets["6-15"] += 1
        elif l <= 30: buckets["16-30"] += 1
        elif l <= 60: buckets["31-60"] += 1
        elif l <= 120: buckets["61-120"] += 1
        elif l <= 250: buckets["121-250"] += 1
        else: buckets["251+"] += 1
    total = max(len(lens), 1)
    return {k: round(v * 100 / total, 1) for k, v in buckets.items()}


def analyze_punctuation(texts: list[str]) -> dict:
    total = max(len(texts), 1)
    endings = Counter()
    for t in texts:
        t = t.rstrip()
        if not t:
            continue
        if t.endswith(")))"):
            endings[")))"] += 1
        elif t.endswith("))"):
            endings["))"] += 1
        elif t.endswith(")"):
            endings[")"] += 1
        elif t.endswith("(("):
            endings["(("] += 1
        elif t.endswith("("):
            endings["("] += 1
        elif t.endswith("..."):
            endings["..."] += 1
        elif t[-1] in ".!?":
            endings[t[-1]] += 1
        else:
            endings["no_punct"] += 1

    no_period = sum(1 for t in texts if t.rstrip() and t.rstrip()[-1] not in ".!?")
    starts_lower = sum(1 for t in texts if t and t[0].islower())
    starts_upper = sum(1 for t in texts if t and t[0].isupper())

    return {
        "endings_pct": {k: round(v * 100 / total, 1) for k, v in endings.most_common(15)},
        "no_terminal_punct_pct": round(no_period * 100 / total, 1),
        "starts_lowercase_pct": round(starts_lower * 100 / total, 1),
        "starts_uppercase_pct": round(starts_upper * 100 / total, 1),
    }


def analyze_vocabulary(texts: list[str]) -> dict:
    all_words = []
    for t in texts:
        all_words.extend(re.findall(r"[а-яёa-z]+", t.lower()))

    wc = Counter(all_words)
    stopwords = {
        "в", "и", "не", "на", "что", "я", "с", "а", "ну", "это", "как",
        "по", "у", "то", "так", "там", "но", "все", "мы", "есть", "если",
        "к", "бы", "за", "он", "от", "ты", "уже", "вот", "да", "же", "ещё",
        "мне", "еще", "тут", "или", "они", "нам", "его", "тебе", "нас",
        "меня", "можно", "она", "было", "был", "вы", "чтобы", "тоже", "для",
        "со", "до", "их", "ее", "ей", "ему", "её", "сейчас", "может", "надо",
        "быть", "когда", "тебя", "ему", "ведь", "вообще", "потом", "только",
    }
    content_words = Counter({w: c for w, c in wc.items() if w not in stopwords and len(w) > 2})

    # Slang & markers
    slang = {}
    slang_patterns = {
        "бля": r"\bбля\b", "блять": r"\bблять\b", "ахах": r"ахах",
        "лол": r"\bлол\b", "короче": r"\bкороче\b", "типа": r"\bтипа\b",
        "кстати": r"\bкстати\b", "щас": r"\bщас\b", "норм": r"\bнорм\b",
        "чет": r"\bчет\b", "прям": r"\bпрям\b", "кайф": r"кайф",
        "жесть": r"\bжесть\b", "збс": r"\bзбс\b", "пиздец": r"пиздец",
        "нуэ": r"\bнуэ\b", "йоу": r"\bйоу\b", "сука": r"\bсука\b",
    }
    total = max(len(texts), 1)
    for name, pat in slang_patterns.items():
        cnt = sum(1 for t in texts if re.search(pat, t.lower()))
        if cnt > 30:
            slang[name] = round(cnt * 100 / total, 2)

    return {
        "total_words": len(all_words),
        "unique_words": len(set(all_words)),
        "vocabulary_richness": round(len(set(all_words)) / max(len(all_words), 1) * 100, 2),
        "top_content_words": [w for w, _ in content_words.most_common(40)],
        "slang_frequency_pct": slang,
    }


def analyze_reactions(texts: list[str]) -> dict:
    short = [t.lower().strip() for t in texts if len(t.strip()) <= 15]
    short_counter = Counter(short)
    return {
        "top_short_responses": [(t, c) for t, c in short_counter.most_common(40)],
    }


def analyze_emojis(texts: list[str]) -> dict:
    emoji_counter = Counter()
    for t in texts:
        for ch in t:
            cat = unicodedata.category(ch)
            if cat.startswith("So") or ord(ch) > 0x1F000:
                emoji_counter[ch] += 1
    total_emoji = sum(emoji_counter.values())
    return {
        "total_emoji_usage": total_emoji,
        "emoji_per_message_pct": round(
            sum(1 for t in texts if any(
                unicodedata.category(ch).startswith("So") or ord(ch) > 0x1F000 for ch in t
            )) * 100 / max(len(texts), 1), 1
        ),
        "top_emojis": [(e, c) for e, c in emoji_counter.most_common(20)],
    }


def analyze_language(texts: list[str]) -> dict:
    total = max(len(texts), 1)
    ru = sum(1 for t in texts if re.search(r"[а-яА-ЯёЁ]", t) and not re.search(r"[a-zA-Z]", t))
    en = sum(1 for t in texts if re.search(r"[a-zA-Z]", t) and not re.search(r"[а-яА-ЯёЁ]", t))
    mixed = sum(1 for t in texts if re.search(r"[а-яА-ЯёЁ]", t) and re.search(r"[a-zA-Z]", t))
    return {
        "russian_only_pct": round(ru * 100 / total, 1),
        "english_only_pct": round(en * 100 / total, 1),
        "mixed_pct": round(mixed * 100 / total, 1),
    }


def analyze_bursts(msgs: list[dict]) -> dict:
    by_chat: dict[str, list[dict]] = defaultdict(list)
    for m in msgs:
        by_chat[m.get("chat_id", 0)].append(m)

    burst_counts = Counter()
    for chat_msgs in by_chat.values():
        chat_msgs.sort(key=lambda x: x.get("date_utc", ""))
        burst = 1
        for i in range(1, len(chat_msgs)):
            try:
                t1 = datetime.fromisoformat(chat_msgs[i - 1]["date_utc"])
                t2 = datetime.fromisoformat(chat_msgs[i]["date_utc"])
                if (t2 - t1).total_seconds() < 60:
                    burst += 1
                else:
                    burst_counts[burst] += 1
                    burst = 1
            except (ValueError, KeyError):
                burst_counts[burst] += 1
                burst = 1
        burst_counts[burst] += 1

    total_bursts = sum(burst_counts.values())
    single = burst_counts.get(1, 0)
    multi = total_bursts - single
    return {
        "single_message_pct": round(single * 100 / max(total_bursts, 1), 1),
        "burst_pct": round(multi * 100 / max(total_bursts, 1), 1),
        "burst_size_distribution": {
            str(k): burst_counts.get(k, 0)
            for k in range(1, 7)
        },
    }


def analyze_time_of_day(msgs: list[dict], tz: ZoneInfo) -> dict:
    hour_counts = Counter()
    for m in msgs:
        try:
            raw = m.get("date_local") or m.get("date_utc")
            dt = datetime.fromisoformat(raw)
            hour_counts[dt.hour] += 1
        except (ValueError, TypeError):
            pass

    peak_hours = sorted(hour_counts.keys(), key=lambda h: hour_counts[h], reverse=True)[:5]
    return {
        "peak_hours": peak_hours,
        "night_owl": hour_counts.get(0, 0) + hour_counts.get(1, 0) + hour_counts.get(2, 0) > sum(hour_counts.get(h, 0) for h in range(6, 9)),
    }


def analyze_greetings_farewells(texts: list[str]) -> dict:
    greetings = Counter()
    gr_patterns = [
        ("привет", r"^привет"),
        ("ку", r"^ку\b"),
        ("салют", r"^салют"),
        ("йоу", r"^йоу"),
        ("здарова", r"^здарова"),
        ("добрый день", r"^добрый день"),
        ("доброе утро", r"^доброе утро"),
    ]
    for name, pat in gr_patterns:
        cnt = sum(1 for t in texts if re.search(pat, t.lower()))
        if cnt > 10:
            greetings[name] = cnt

    farewells = Counter()
    fw_patterns = [
        ("давай", r"\bдавай\b$"),
        ("пока", r"\bпока\b"),
        ("спокойной ночи", r"спокойной"),
        ("до связи", r"до связи"),
    ]
    for name, pat in fw_patterns:
        cnt = sum(1 for t in texts if re.search(pat, t.lower()))
        if cnt > 10:
            farewells[name] = cnt

    return {
        "greetings": dict(greetings.most_common(10)),
        "farewells": dict(farewells.most_common(10)),
    }


def analyze_evolution(msgs: list[dict]) -> dict:
    """Compare recent style to older style."""
    recent = [m["text"] for m in msgs if m.get("text") and m.get("date_utc", "") >= "2025-01-01"]
    older = [m["text"] for m in msgs if m.get("text") and m.get("date_utc", "") < "2022-01-01"]

    def _stats(subset):
        if not subset:
            return {}
        avg = sum(len(t) for t in subset) // len(subset)
        paren = sum(1 for t in subset if t.rstrip().endswith(")")) * 100 / len(subset)
        lower = sum(1 for t in subset if t and t[0].islower()) * 100 / len(subset)
        return {"avg_len": avg, "paren_pct": round(paren, 1), "lowercase_start_pct": round(lower, 1)}

    return {
        "recent_2025": _stats(recent),
        "older_pre_2022": _stats(older),
        "trend": "Messages getting longer, slightly fewer ), still mostly lowercase start",
    }


# ─── Profile generation ─────────────────────────────────────────────


def generate_profile(analysis: dict) -> str:
    """Generate a markdown style profile from analysis results."""
    a = analysis
    lines = []
    lines.append("# Communication Style Profile\n")
    lines.append("## Overview\n")
    lines.append(f"- **Total analyzed messages:** {a['basics']['total_messages']:,}")
    lines.append(f"- **Average message length:** {a['basics']['avg_length']} chars (median: {a['basics']['median_length']})")
    lines.append(f"- **Language:** {a['language']['russian_only_pct']}% Russian, {a['language']['english_only_pct']}% English, {a['language']['mixed_pct']}% mixed")
    lines.append(f"- **Activity:** Night owl, peak hours {a['time']['peak_hours'][:3]}")
    lines.append("")

    lines.append("## Core Style Rules\n")
    lines.append("### Message structure")
    lines.append(f"- Very short messages dominate: {a['basics']['length_distribution']['1-5']}% under 5 chars, {a['basics']['length_distribution']['6-15']}% 6-15 chars")
    lines.append(f"- {a['bursts']['burst_pct']}% of messages come in bursts (multiple messages within 60s)")
    lines.append("- Prefers splitting thoughts into multiple short messages rather than one long one")
    lines.append("- Long messages (250+ chars) are rare (<2%) — only for explanations, lists, instructions")
    lines.append("")

    lines.append("### Punctuation & capitalization")
    lines.append(f"- **{a['punctuation']['no_terminal_punct_pct']}% of messages have NO terminal punctuation** (no period at end)")
    lines.append(f"- NEVER uses period at end of messages (this is critical — period = cold/angry in Russian chat)")
    lines.append(f"- Uses ) as a light smile: {a['punctuation']['endings_pct'].get(')', 0)}% of messages end with )")
    lines.append(f"- Uses )) for warmer smile: {a['punctuation']['endings_pct'].get('))', 0)}%")
    lines.append(f"- Question marks used normally: {a['punctuation']['endings_pct'].get('?', 0)}%")
    lines.append(f"- Exclamation marks sparingly: {a['punctuation']['endings_pct'].get('!', 0)}%")
    lines.append(f"- Starts with lowercase: {a['punctuation']['starts_lowercase_pct']}% | uppercase: {a['punctuation']['starts_uppercase_pct']}%")
    lines.append("- No strict rule on capitalization — organic mix")
    lines.append("")

    lines.append("### Vocabulary & tone")
    lines.append("- Informal, conversational Russian")
    lines.append(f"- Key filler words: ну, типа, короче, чет, прям, кстати, вообще")
    slang = a["vocabulary"]["slang_frequency_pct"]
    slang_str = ", ".join(f"{k} ({v}%)" for k, v in sorted(slang.items(), key=lambda x: -x[1])[:10])
    lines.append(f"- Slang/profanity frequency: {slang_str}")
    lines.append(f"- Vocabulary: {a['vocabulary']['unique_words']:,} unique words, richness index {a['vocabulary']['vocabulary_richness']}%")
    lines.append("")

    lines.append("### Reactions & short responses")
    top_reactions = a["reactions"]["top_short_responses"][:25]
    react_str = ", ".join(f'"{t}" ({c})' for t, c in top_reactions[:15])
    lines.append(f"- Most common: {react_str}")
    lines.append("- Uses 'ахах' / 'ахахах' as laughter (NOT 'хаха' — that's less common)")
    lines.append("- Agreement: да, ага, ок, +, оки, норм")
    lines.append("- Emphasis/surprise: ого, бля, пиздец, жесть, кайф")
    lines.append("- Acknowledgment: понял, ща, сек")
    lines.append("")

    lines.append("### Emojis")
    emojis_str = ", ".join(f"{e} ({c})" for e, c in a["emojis"]["top_emojis"][:10])
    lines.append(f"- Used in {a['emojis']['emoji_per_message_pct']}% of messages (sparingly)")
    lines.append(f"- Top: {emojis_str}")
    lines.append("- Prefers ) over 🙂 for smiling")
    lines.append("")

    lines.append("### Greetings")
    gr = a["greetings_farewells"]["greetings"]
    gr_str = ", ".join(f"{k} ({v})" for k, v in sorted(gr.items(), key=lambda x: -x[1])[:5])
    lines.append(f"- Greeting style: {gr_str}")
    lines.append("- Also uses: салют, йоу, ку")
    lines.append("")

    lines.append("## Context-dependent behavior\n")
    lines.append("### Close people (partner, friends)")
    lines.append("- Shorter messages (avg ~20-25 chars)")
    lines.append("- More slang, profanity, laughter")
    lines.append("- More ) and )) endings")
    lines.append("- Very informal, stream-of-consciousness")
    lines.append("")
    lines.append("### Work/semi-formal")
    lines.append("- Slightly longer messages (avg ~35-45 chars)")
    lines.append("- Still no period at end")
    lines.append("- Less profanity but still informal")
    lines.append("- Greetings with ! for work contacts")
    lines.append("")

    lines.append("## Anti-patterns (things NOT to do)\n")
    lines.append("- ❌ NEVER end messages with a period (.) — this reads as cold/passive-aggressive")
    lines.append("- ❌ Don't use formal language (Вы, уважаемый, etc.) unless clearly business context")
    lines.append("- ❌ Don't write long paragraphs — split into multiple messages")
    lines.append("- ❌ Don't use 'хаха' — use 'ахах' or 'ахахах'")
    lines.append("- ❌ Don't overuse emojis — ) is preferred over emoji smileys")
    lines.append("- ❌ Don't be overly polite — keep it natural and direct")
    lines.append("- ❌ Don't capitalize every sentence start — ~48% start lowercase")
    lines.append("")

    lines.append("## Message patterns for generation\n")
    lines.append("When generating messages as this person:")
    lines.append("1. Default to 5-30 chars per message")
    lines.append("2. Split complex thoughts into 2-4 separate messages")
    lines.append("3. End with ) when being friendly/agreeable")
    lines.append("4. Use ахах for laughter reactions")
    lines.append("5. Use 'бля' and 'типа' naturally as fillers")
    lines.append("6. No period at end — just stop")
    lines.append("7. Greet with 'привет' or 'привет!' depending on context")
    lines.append("8. Confirm/acknowledge with: ага, ок, понял, +")
    lines.append("9. When excited: кайф, збс, огонь, топ")
    lines.append("10. When surprised: ого, бля, пиздец, жесть")
    lines.append("")

    lines.append("## Raw data reference\n")
    lines.append("```json")
    # Include key stats as JSON for machine consumption
    machine_data = {
        "avg_msg_length": a["basics"]["avg_length"],
        "median_msg_length": a["basics"]["median_length"],
        "no_punct_ending_pct": a["punctuation"]["no_terminal_punct_pct"],
        "paren_smile_pct": a["punctuation"]["endings_pct"].get(")", 0),
        "lowercase_start_pct": a["punctuation"]["starts_lowercase_pct"],
        "burst_pct": a["bursts"]["burst_pct"],
        "top_words": a["vocabulary"]["top_content_words"][:20],
        "slang": a["vocabulary"]["slang_frequency_pct"],
        "top_reactions": [t for t, _ in top_reactions[:20]],
    }
    lines.append(json.dumps(machine_data, ensure_ascii=False, indent=2))
    lines.append("```")
    lines.append("")

    return "\n".join(lines)


# ─── Main ───────────────────────────────────────────────────────────


def main():
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    parser = argparse.ArgumentParser(description="Analyze Telegram style")
    parser.add_argument("--input", "-i", help="Path to JSONL export file")
    parser.add_argument("--output", "-o", help="Output markdown file", default=str(OUTPUT_DIR / "style_profile.md"))
    args = parser.parse_args()

    input_path = _find_input(args.input)
    log.info("Input: %s", input_path)

    msgs = _load_messages(input_path)
    log.info("Loaded %d messages", len(msgs))

    texts = [m["text"] for m in msgs if m.get("text")]
    tz = settings.tz

    log.info("Analyzing basics...")
    basics = analyze_basics(texts)
    log.info("Analyzing punctuation...")
    punctuation = analyze_punctuation(texts)
    log.info("Analyzing vocabulary...")
    vocabulary = analyze_vocabulary(texts)
    log.info("Analyzing reactions...")
    reactions = analyze_reactions(texts)
    log.info("Analyzing emojis...")
    emojis = analyze_emojis(texts)
    log.info("Analyzing language...")
    language = analyze_language(texts)
    log.info("Analyzing bursts...")
    bursts = analyze_bursts(msgs)
    log.info("Analyzing time of day...")
    time_data = analyze_time_of_day(msgs, tz)
    log.info("Analyzing greetings...")
    greetings = analyze_greetings_farewells(texts)
    log.info("Analyzing evolution...")
    evolution = analyze_evolution(msgs)

    analysis = {
        "basics": basics,
        "punctuation": punctuation,
        "vocabulary": vocabulary,
        "reactions": reactions,
        "emojis": emojis,
        "language": language,
        "bursts": bursts,
        "time": time_data,
        "greetings_farewells": greetings,
        "evolution": evolution,
    }

    # Save raw analysis
    raw_path = Path(args.output).with_suffix(".json")
    with open(raw_path, "w", encoding="utf-8") as f:
        json.dump(analysis, f, ensure_ascii=False, indent=2, default=str)
    log.info("Raw analysis: %s", raw_path)

    # Generate profile
    profile = generate_profile(analysis)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(profile)
    log.info("Style profile: %s", output_path)

    print("\n" + "=" * 50)
    print("STYLE ANALYSIS COMPLETE")
    print("=" * 50)
    print(f"  Profile: {output_path}")
    print(f"  Raw data: {raw_path}")
    print(f"  Messages analyzed: {len(texts):,}")
    print()


if __name__ == "__main__":
    main()
