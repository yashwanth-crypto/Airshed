"""Fill the official SIH 2026 idea template with the Airshed content.

Edits the packed XML rather than going through python-pptx, because the
template's formatting lives in run properties that `text_frame.text = ...`
collapses to a single unstyled run.

The template's instruction slide says the idea pointers may not be changed, so
**every** pointer line the template ships with is preserved -- the underlined
section header and each prompt beneath it. Our content is added under the
prompt it answers, rather than replacing it.
"""

import re
import shutil
import zipfile
from pathlib import Path

SRC = Path("template.pptx")
OUT = Path("Airshed-SIH2026-Idea.pptx")
WORK = Path("unpacked")

HEADER_PT = 2000   # the underlined section pointer
PROMPT_PT = 1600   # the template's prompt lines, kept as sub-headings
BODY_PT = 1300     # our content

TITLE_LINES = [
    "Problem Statement ID – SIH26082",
    "Problem Statement Title- Air Pollution–Weather Coupled Forecasting "
    "System for Delhi NCR",
    "Theme- Disaster Management",
    "PS Category- Software",
    "Team ID- <your team ID>",
    "Team Name (Registered on portal)- <your team name>",
]

# One entry per template prompt, in the order the template lists them.
CONTENT = {
    2: [
        ["CAMS (Copernicus) forecasts Delhi's air days ahead, free — but at "
         "40 km and biased low over India. We do not rebuild that physics; we "
         "train a correction layer on top of it.",
         "CAMS supplies the future — winds, transport, regional build-up. Our "
         "layer supplies the local — bias correction and sub-grid detail."],
        ["A model trained only on station history is autoregressive and "
         "collapses past 24 h. Feeding it a physics forecast of the future "
         "fixes that structurally, which is what makes 72 h possible.",
         "Output is the decision, not a number: probability each GRAP stage is "
         "reached, with lead time and driver attribution."],
        ["The baseline is external and published, so beating it by a measured "
         "margin is a result rather than a claim.",
         "Coupled both ways: pollution also improves the visibility (fog) "
         "forecast, measured against airport observations."],
    ],
    3: [
        ["Python · LightGBM · Polars · DuckDB · FastAPI · MapLibre + deck.gl. "
         "No GPU and no database server — it runs on one laptop.",
         "All data free: CAMS and GFS via Open-Meteo, CPCB ground truth via "
         "OpenAQ (77 NCR stations), METAR visibility, NASA FIRMS fires."],
        ["Ingest → align every source on one hourly UTC index → supervised "
         "table → correct → decide → serve. Below the cache nothing makes a "
         "network call, so an upstream outage degrades the demo, not kills it.",
         "Direct multi-horizon quantile heads — 3 horizons × 3 quantiles, no "
         "recursive rollout. Conformal calibration; wind-aware graph for "
         "spatial downscaling; SHAP grouped into human causes."],
    ],
    4: [
        ["Built and running end-to-end: 77 stations, 9M cached rows, live "
         "dashboard with 72 h forecast, historical replay and GRAP "
         "probabilities. Zero data cost."],
        ["Only one winter of trainable ground truth exists, so effects of a "
         "few percent cannot yet be separated from noise.",
         "CPCB stations go offline; archived and live CAMS differ. Both are "
         "measured and stated on the dashboard rather than hidden."],
        ["Persistence in every results table, whole-season holdout, and "
         "rolling-origin folds — a claim smaller than its own spread is "
         "reported as 'cannot tell', not as a win.",
         "Everything is cached and re-runnable offline; a second episode "
         "season (Nov 2026) resolves the open questions."],
    ],
    5: [
        ["GRAP measures take days to take effect. We catch Stage III with "
         "72 hours of lead — the difference between a forecast and a warning.",
         "Serves CAQM, DPCC and 30M+ residents of the NCR."],
        ["Health: earlier school closures, construction halts and advisories "
         "during severe episodes.",
         "Economic: fewer diverted flights at IGI and fewer highway pile-ups, "
         "from fog risk we can forecast three days out.",
         "Measured: 31.5% lower error than raw CAMS and 20.6% lower than "
         "persistence, on 5 of 5 seasonal folds. The same correction applies "
         "to the ministry's own model as easily as to CAMS."],
    ],
    6: [
        ["CAMS global forecast — Copernicus Atmosphere Monitoring Service, "
         "accessed free and keyless via the Open-Meteo Air Quality API.",
         "CPCB CAAQMS ground truth via OpenAQ; GFS meteorology and archived "
         "past runs via Open-Meteo's Historical Forecast and Previous Runs APIs.",
         "NASA FIRMS VIIRS/MODIS active fire detections for stubble burning; "
         "Iowa State IEM archive for measured METAR visibility at VIDP.",
         "GRAP stage thresholds transcribed from the CAQM published schedule; "
         "CPCB AQI breakpoints for the 24-hour mean."],
    ],
}


def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def unpack() -> None:
    if WORK.exists():
        shutil.rmtree(WORK)
    with zipfile.ZipFile(SRC) as z:
        z.extractall(WORK)


def slide_path(n: int) -> Path:
    return WORK / "ppt" / "slides" / f"slide{n}.xml"


def fill_title() -> None:
    p = slide_path(1)
    xml = p.read_text(encoding="utf-8")
    for old, new in zip(
        ["Problem Statement ID –", "Problem Statement Title-", "Theme-",
         "PS Category- Software/Hardware", "Team ID-",
         "Team Name (Registered on portal)"],
        TITLE_LINES,
    ):
        needle = f"<a:t>{esc(old)}</a:t>"
        if needle not in xml:
            raise SystemExit(f"  ! not found on title slide: {old}")
        xml = xml.replace(needle, f"<a:t>{esc(new)}</a:t>", 1)
    p.write_text(xml, encoding="utf-8")
    print("  slide 1: title page")


def content_box(xml: str) -> str:
    m = re.search(r'name="TextBox \d+".*?</p:txBody>', xml, re.S)
    if not m:
        raise SystemExit("  ! no content TextBox")
    return m.group(0)


def set_size(para: str, pt: int) -> str:
    return re.sub(r'sz="\d+"', f'sz="{pt}"', para)


def body_from(proto: str, text: str, pt: int) -> str:
    """A content bullet derived from a template paragraph, unbold, un-underlined."""
    para = proto.replace(' u="sng"', "").replace(' b="1"', "")
    para = set_size(para, pt)
    return re.sub(r"<a:t>.*?</a:t>", f"<a:t>{esc(text)}</a:t>", para, count=1, flags=re.S)


def set_bold(para: str) -> str:
    """Force the first run of a paragraph bold, without duplicating `b`.

    Inserting b="1" blindly next to an existing b="0" produces the attribute
    twice. That is malformed XML and PowerPoint refuses to open the file --
    caught by the validator rather than by anything visible.
    """
    m = re.search(r"<a:rPr[^>]*>", para)
    if not m:
        return para
    tag = m.group(0)
    cleaned = re.sub(r'\s+b="[^"]*"', "", tag)
    bolded = cleaned.replace("<a:rPr", '<a:rPr b="1"', 1)
    return para.replace(tag, bolded, 1)


def fill_content(n: int, groups: list[list[str]]) -> None:
    """Keep every template pointer; add our bullets under the prompt each answers."""
    p = slide_path(n)
    xml = p.read_text(encoding="utf-8")
    box = content_box(xml)
    paras = re.findall(r"<a:p>.*?</a:p>", box, re.S)
    text_paras = [q for q in paras if re.search(r"<a:t>\s*\S", q)]

    # The section header is identified by its underline, not by position:
    # only slide 2 ships one, and on the rest every text paragraph is a prompt.
    if text_paras and 'u="sng"' in text_paras[0]:
        header, prompts = text_paras[0], text_paras[1:]
    else:
        header, prompts = None, text_paras
    if len(prompts) != len(groups):
        raise SystemExit(
            f"  ! slide {n}: template has {len(prompts)} prompts, "
            f"content supplies {len(groups)}"
        )

    out = [set_size(header, HEADER_PT)] if header else []
    for prompt, bullets in zip(prompts, groups):
        # The prompt stays, bolded, as the sub-heading for our answer.
        kept = set_size(prompt, PROMPT_PT)
        kept = set_bold(kept)
        out.append(kept)
        out += [body_from(prompt, b, BODY_PT) for b in bullets]

    head = box[: box.index("<a:p>")]
    xml = xml.replace(box, head + "".join(out) + "</p:txBody>", 1)
    p.write_text(xml, encoding="utf-8")
    print(f"  slide {n}: {len(prompts)} pointers kept, "
          f"{sum(len(g) for g in groups)} points added")


def drop_instruction_slide() -> None:
    pres = WORK / "ppt" / "presentation.xml"
    xml = pres.read_text(encoding="utf-8")
    ids = re.findall(r"<p:sldId[^>]*/>", xml)
    if len(ids) >= 7:
        pres.write_text(xml.replace(ids[6], "", 1), encoding="utf-8")
        print(f"  removed the instruction slide ({len(ids)} -> {len(ids)-1})")


def pack() -> None:
    OUT.unlink(missing_ok=True)
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(WORK.rglob("*")):
            if f.is_file():
                z.write(f, f.relative_to(WORK).as_posix())
    print(f"  wrote {OUT} ({OUT.stat().st_size/1024:.0f} KB)")


if __name__ == "__main__":
    unpack()
    drop_instruction_slide()
    fill_title()
    for n, groups in CONTENT.items():
        fill_content(n, groups)
    pack()
