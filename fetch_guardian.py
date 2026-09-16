# -*- coding: utf-8 -*-
"""Плюс вопросы к стартовому набору: свежие факты с The Guardian (искусство, спорт, технологии, рестораны).

Запуск (из той же папки, где лежат fetch_fresh.py и check_fresh.py):
    python fetch_guardian.py --new-target 25

Результат: дописывает новые строки в data/fresh.jsonl, не трогая уже существующие записи
(дедуп по полю evidence — как в fetch_fresh.py --append).

Как собирается:
1. Guardian Content API (open-platform.theguardian.com) отдаёт статьи с 1 января 2025 года
   из выбранных разделов с полным текстом (show-fields=body).
2. Текст режется на абзацы; отбрасываются слишком короткие/длинные и анонсовые формулировки
   ("will", "is set to", "upcoming" и т.п. — тот же фильтр SKIP, что в fetch_fresh.py).
3. Дешёвая модель делает из абзаца вопрос с коротким ответом (тот же формат, что и для вики-фактов).
4. Сильная модель отвечает без инструментов; в файл попадают только вопросы, где она ошиблась.

Переиспользует общую инфраструктуру из fetch_fresh.py (ключ OpenRouter, chat(), json_from(),
normalize(), good_pair(), strong_answer(), подсчёт денег) — эту переменную не трогаем,
fetch_guardian.py должен лежать рядом с fetch_fresh.py.

Нужен свежий ключ Guardian: открытая регистрация на open-platform.theguardian.com/access,
ключ кладём в .env рядом с OPENROUTER_API_KEY:
    GUARDIAN_API_KEY=...

Этот скрипт не тестировался живьём той средой, где его писали (у неё нет доступа
к content.guardianapis.com) — первый прогон стоит сделать у себя и прислать вывод,
если что-то пойдёт не так, поправим.
"""
import os, re, sys, json, time, random, argparse, html, datetime
from pathlib import Path
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fetch_fresh as base  # ключ, chat(), json_from(), normalize(), good_pair(), strong_answer(), COST

sys.stdout.reconfigure(encoding="utf-8")
HERE = Path(__file__).resolve().parent

GUARDIAN_API_KEY = os.getenv("GUARDIAN_API_KEY")
if not GUARDIAN_API_KEY:
    env_path = HERE / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if line.startswith("GUARDIAN_API_KEY=") and not GUARDIAN_API_KEY:
                GUARDIAN_API_KEY = line.split("=", 1)[1].strip()
assert GUARDIAN_API_KEY, "нужен GUARDIAN_API_KEY в .env — ключ на open-platform.theguardian.com/access"

GUARDIAN_URL = "https://content.guardianapis.com/search"
FROM_DATE = "2025-01-01"
TO_DATE = datetime.date.today().isoformat()

# Если "рестораны" даёт мало толковых кандидатов или не то по смыслу (в food вперемешку
# ещё рецепты и вино) — посмотрите точный тег через
#   GET https://content.guardianapis.com/tags?q=restaurant&api-key=ВАШ_КЛЮЧ
# и замените здесь "q" на, например, {"tag": "lifeandstyle/restaurants"}.
SECTIONS = {
    "искусство": {"section": "artanddesign"},
    "спорт": {"section": "sport"},
    "технологии": {"section": "technology"},
    "рестораны": {"section": "food", "q": "restaurant review"},
}

TAG_RE = re.compile(r"<[^>]+>")
SKIP = base.SKIP  # тот же фильтр анонсов/дат рождения, что в fetch_fresh.py


def guardian_page(section_params, page, attempts=4):
    params = {**section_params, "from-date": FROM_DATE, "to-date": TO_DATE, "order-by": "newest",
              "page-size": 20, "page": page, "show-fields": "body,standfirst", "api-key": GUARDIAN_API_KEY}
    problem = "нет ответа"
    for attempt in range(attempts):
        try:
            r = requests.get(GUARDIAN_URL, params=params, timeout=20)
            if r.status_code == 200:
                return r.json()["response"]
            problem = f"HTTP {r.status_code}: {r.text[:200]}"
            if r.status_code not in (429, 500, 502, 503, 504):
                break
        except requests.RequestException as e:
            problem = type(e).__name__
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"Guardian API: {problem}")


def paragraphs(body_html):
    out = []
    for m in re.finditer(r"<p>(.*?)</p>", body_html or "", re.S):
        text = html.unescape(TAG_RE.sub(" ", m.group(1)))
        text = " ".join(text.split())
        if 60 <= len(text) <= 420 and not SKIP.search(text):
            out.append(text)
    return out


def collect_facts(used):
    facts = []
    for topic, params in SECTIONS.items():
        got = 0
        for page in (1, 2):
            try:
                data = guardian_page(params, page)
            except RuntimeError as e:
                print(f"{topic}: страница {page} пропущена — {e}", flush=True)
                break
            for art in data.get("results", []):
                body = (art.get("fields") or {}).get("body", "")
                for p in paragraphs(body)[:3]:
                    if p in used:
                        continue
                    facts.append({"topic": topic, "title": art.get("webTitle", ""),
                                  "date": (art.get("webPublicationDate") or "")[:10],
                                  "url": art.get("webUrl", ""), "text": p})
                    got += 1
            if page >= data.get("pages", 1):
                break
        print(f"{topic:12s} {got:4d} фактов-кандидатов", flush=True)
    return facts


GEN_PROMPT = """Each passage below is a paragraph from a Guardian news article (section and publish date given in parentheses, then the article title). For each passage write one quiz question with a short unambiguous answer.
Rules: the answer is a single entity, at most five words: a person's name, a number, a title, a place, the name of a dish/restaurant/exhibition, or a date, nothing else;
the answer must appear verbatim in the passage; the question asks about one specific, checkable detail described in the passage, never "what happened" or "who is mentioned";
the question is self-contained: name the general subject (sport / technology / art / restaurant) and enough of the event or work to identify it without reading the article, reusing details from the title if useful;
never put the answer into the question; skip passages that only preview or promise something ("will open", "is set to", "coming soon") and passages without one clear short checkable detail.
Return only a JSON array of objects {"i": <passage index>, "question": "...", "answer": "..."}."""


def generate(facts):
    items = []
    for start in range(0, len(facts), 8):
        batch = facts[start:start + 8]
        listing = "\n".join(f'[{start + k}] ({f["topic"]}, {f["date"]}, "{f["title"]}") {f["text"]}'
                             for k, f in enumerate(batch))
        try:
            for obj in base.json_from(base.chat([{"role": "system", "content": GEN_PROMPT},
                                                  {"role": "user", "content": listing}], base.CHEAP)):
                i, q, a = int(obj["i"]), str(obj["question"]).strip(), str(obj["answer"]).strip()
                if 0 <= i < len(facts) and base.good_pair(q, a, facts[i]["text"]):
                    items.append({**facts[i], "question": q, "answer": a})
        except (ValueError, KeyError, TypeError, RuntimeError) as e:
            print("  батч пропущен:", e, flush=True)
    return items


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--new-target", type=int, default=25, help="сколько новых вопросов добавить (минимум по заданию — 20)")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", default="data/fresh.jsonl", help="куда дописывать (должен уже существовать со стартовым набором)")
    args = ap.parse_args()
    random.seed(args.seed)

    out = HERE / args.out
    if not out.exists():
        raise SystemExit(f"{out} не найден — сначала положите туда стартовый fresh_2026.jsonl (125 вопросов)")
    existing = [json.loads(l) for l in out.read_text(encoding="utf-8").splitlines() if l.strip()]
    used = {e["evidence"] for e in existing}
    start_n = len(existing)
    print(f"в файле уже {start_n} вопросов, цель — добавить ещё {args.new_target}", flush=True)

    facts = collect_facts(used)
    random.shuffle(facts)
    print(f"кандидатов после сбора: {len(facts)}", flush=True)
    if not facts:
        raise SystemExit("кандидатов нет — проверьте GUARDIAN_API_KEY и доступность content.guardianapis.com")

    items = generate(facts)
    print(f"вопросов, прошедших фильтры формата: {len(items)}", flush=True)

    kept = 0
    with out.open("a", encoding="utf-8") as f:
        for it in items:
            if kept >= args.new_target:
                break
            try:
                sonnet = base.strong_answer(it["question"])
            except RuntimeError as e:
                print("  пропуск, сеть:", e, flush=True)
                continue
            if base.normalize(it["answer"]) in base.normalize(sonnet):
                continue  # Sonnet и так угадал — вопрос не свежий, не пишем
            row = {"id": f"guardian-{kept}", "source": "guardian", "question": it["question"], "answer": it["answer"],
                   "evidence": it["text"], "page": it["title"], "url": it["url"], "sonnet_answer": sonnet}
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            f.flush()
            kept += 1
            print(f"  {it['question'][:90]} -> {it['answer']} | sonnet: {sonnet[:40]}", flush=True)

    print(f"\nдобавлено {kept} новых вопросов, всего теперь {start_n + kept}; потрачено ${base.COST:.3f}", flush=True)


if __name__ == "__main__":
    main()
