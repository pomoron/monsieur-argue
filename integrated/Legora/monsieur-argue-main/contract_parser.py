"""
contract_parser.py
------------------
Optional pre-session agent: reads a contract PDF, extracts key terms and
party-beneficiary analysis, then merges findings into scenario and persona dicts.

Standalone usage:
    python contract_parser.py --pdf contract.pdf [--save] [--dump]

Module usage:
    from contract_parser import parse_contract, augment_inputs
    contract_data = parse_contract(config, pdf_path)
    scenario, persona = augment_inputs(contract_data, scenario, persona)
    # ai_side is inferred automatically from persona["represents"] and scenario["your_side"]
    # Override only if needed: augment_inputs(contract_data, scenario, persona, ai_side="PARTY_A")

Requires: pip install pypdf
"""

import argparse
import json
import os
import sys

from llm_client import call_llm


# ── PDF extraction ─────────────────────────────────────────────────────────────

def extract_pdf_text(pdf_path: str) -> str:
    """Extract all text from a PDF using pypdf."""
    try:
        from pypdf import PdfReader
    except ImportError:
        raise RuntimeError("pypdf is not installed. Run: pip install pypdf")

    if not os.path.exists(pdf_path):
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    reader = PdfReader(pdf_path)
    pages  = []
    for i, page in enumerate(reader.pages):
        text = page.extract_text() or ""
        if text.strip():
            pages.append(f"[Page {i + 1}]\n{text}")

    if not pages:
        raise ValueError(
            f"No extractable text found in {pdf_path}. "
            "The PDF may be scanned/image-based."
        )
    return "\n\n".join(pages)


# ── LLM extraction ─────────────────────────────────────────────────────────────

_EXTRACTION_SYSTEM = """\
You are a specialist legal analyst. You will be given the text of a contract.
Extract structured information to support a negotiation training exercise.

The negotiation has TWO sides:
  - PARTY_A: the Buyer, Purchaser, Client, Claimant, or first named party
  - PARTY_B: the Seller, Vendor, Supplier, Defendant, or second named party

Return ONLY valid JSON in this exact schema:

{
  "contract_title": "<title or brief description>",
  "party_a_label": "<how the contract names Party A>",
  "party_b_label": "<how the contract names Party B>",
  "agreed_terms": [
    {
      "clause": "<clause number or section>",
      "description": "<plain-English summary>",
      "favours": "PARTY_A" | "PARTY_B" | "NEUTRAL",
      "reason": "<why>"
    }
  ],
  "contested_terms": [
    {
      "issue": "<name of contested point>",
      "clause": "<clause number if identifiable>",
      "current_drafting": "<what the contract currently says>",
      "party_a_concern": "<why Party A might push back>",
      "party_b_concern": "<why Party B might push back>",
      "favours": "PARTY_A" | "PARTY_B" | "NEUTRAL",
      "notes": "<market standard, legal risk, or commercial context>"
    }
  ],
  "party_a_risks":     ["<risk or exposure for Party A>"],
  "party_b_risks":     ["<risk or exposure for Party B>"],
  "party_a_strengths": ["<clause or term advantageous to Party A>"],
  "party_b_strengths": ["<clause or term advantageous to Party B>"]
}

Be specific — quote clause numbers and actual language where possible.
Focus on commercially and legally significant terms.
"""


def _repair_truncated_json(raw: str) -> dict:
    """
    Best-effort recovery when the LLM response was cut off mid-JSON.
    Strips the incomplete trailing content and closes any open arrays/objects
    so we can still parse what arrived cleanly.

    Returns a dict (possibly with fewer items than intended) or raises
    ValueError if even the partial content cannot be salvaged.
    """
    # Walk backward to the last complete top-level value that ends with },
    # then close any open arrays and the root object.
    depth_obj = 0
    depth_arr = 0
    in_string = False
    escape    = False
    last_safe = 0  # index of last char that completed a top-level list item

    for i, ch in enumerate(raw):
        if escape:
            escape = False
            continue
        if ch == "\\" and in_string:
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth_obj += 1
        elif ch == "}":
            depth_obj -= 1
            if depth_obj == 1 and depth_arr == 1:
                # We just closed an object inside the top-level array
                last_safe = i + 1
        elif ch == "[":
            depth_arr += 1
        elif ch == "]":
            depth_arr -= 1

    if last_safe == 0:
        raise ValueError("Could not find any complete JSON objects in truncated output.")

    # Slice to last complete item and close open structures
    salvaged = raw[:last_safe].rstrip().rstrip(",")
    # Count unclosed brackets to close them
    open_arrs = salvaged.count("[") - salvaged.count("]")
    open_objs = salvaged.count("{") - salvaged.count("}")
    salvaged += "]" * open_arrs + "}" * open_objs

    return json.loads(salvaged)


def extract_contract_terms(config: dict, pdf_text: str) -> dict:
    """Send PDF text to LLM; return structured extraction dict."""
    MAX_CHARS = 100_000
    if len(pdf_text) > MAX_CHARS:
        print(f"[contract_parser] Truncating input to {MAX_CHARS:,} chars to fit context window.")
        pdf_text = pdf_text[:MAX_CHARS] + "\n\n[... TEXT TRUNCATED ...]"

    # Use a high token ceiling — contract JSON can be large.
    # 8192 comfortably fits even complex multi-party agreements.
    raw = call_llm(
        config=config,
        system_prompt=_EXTRACTION_SYSTEM,
        messages=[{"role": "user", "content": f"CONTRACT TEXT:\n\n{pdf_text}\n\nReturn JSON only."}],
        temperature=0.1,
        max_tokens=8192,
    )

    raw = raw.strip()
    if raw.startswith("```"):
        lines = raw.split("\n")
        raw   = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

    try:
        return json.loads(raw)
    except json.JSONDecodeError as first_err:
        # Output was likely cut off mid-JSON. Attempt salvage before giving up.
        print(
            f"[contract_parser] JSON parse failed ({first_err}). "
            "Attempting to recover truncated output..."
        )
        try:
            result = _repair_truncated_json(raw)
            print("[contract_parser] Partial recovery succeeded. Some terms may be missing.")
            return result
        except (ValueError, json.JSONDecodeError) as repair_err:
            raise ValueError(
                f"LLM returned non-JSON from contract extraction and recovery failed.\n"
                f"Original error : {first_err}\n"
                f"Recovery error : {repair_err}\n"
                f"Raw output (first 500 chars):\n{raw[:500]}"
            )


# ── Side inference ─────────────────────────────────────────────────────────────

# Roles that map to the first-named / "A" party in a contract
_PARTY_A_ROLES = {
    "BUYER", "PURCHASER", "CLIENT", "CLAIMANT", "PLAINTIFF",
    "APPLICANT", "PETITIONER", "LESSOR", "LANDLORD", "LICENSOR",
}

# Roles that map to the second-named / "B" party in a contract
_PARTY_B_ROLES = {
    "SELLER", "VENDOR", "SUPPLIER", "DEFENDANT", "RESPONDENT",
    "TENANT", "LESSEE", "LICENSEE", "TARGET",
}


def infer_ai_side(persona: dict, scenario: dict, contract_data: dict = None) -> str:
    """
    Determine which contract party (PARTY_A or PARTY_B) the AI represents.

    Resolution order:
    1. persona["represents"]  — explicit field, e.g. "SELLER", "CLAIMANT"
    2. scenario["your_side"]  — free-text fallback, e.g. "Seller (Greenvale...)"
    3. Contract label matching — compare role keywords against the LLM-extracted
       party_a_label / party_b_label strings
    4. Default to PARTY_B with a warning if nothing resolves.

    Args:
        persona:       loaded persona dict
        scenario:      loaded scenario dict
        contract_data: optional output of extract_contract_terms() for label matching

    Returns:
        "PARTY_A" or "PARTY_B"
    """
    # 1. Explicit persona["represents"] field
    represents = persona.get("represents", "").upper().strip()
    if represents:
        if represents in _PARTY_A_ROLES:
            print(f"[contract_parser] AI side inferred from persona['represents']: {represents} -> PARTY_A")
            return "PARTY_A"
        if represents in _PARTY_B_ROLES:
            print(f"[contract_parser] AI side inferred from persona['represents']: {represents} -> PARTY_B")
            return "PARTY_B"
        # Partial match — check if any known keyword appears inside the value
        for role in _PARTY_A_ROLES:
            if role in represents:
                print(f"[contract_parser] AI side inferred from persona['represents'] (partial): {represents} -> PARTY_A")
                return "PARTY_A"
        for role in _PARTY_B_ROLES:
            if role in represents:
                print(f"[contract_parser] AI side inferred from persona['represents'] (partial): {represents} -> PARTY_B")
                return "PARTY_B"

    # 2. scenario["your_side"] free-text scan
    your_side = scenario.get("your_side", "").upper()
    if your_side:
        for role in _PARTY_A_ROLES:
            if role in your_side:
                print(f"[contract_parser] AI side inferred from scenario['your_side']: {scenario['your_side']} -> PARTY_A")
                return "PARTY_A"
        for role in _PARTY_B_ROLES:
            if role in your_side:
                print(f"[contract_parser] AI side inferred from scenario['your_side']: {scenario['your_side']} -> PARTY_B")
                return "PARTY_B"

    # 3. Contract label matching — compare persona role words against extracted labels
    if contract_data:
        label_a = contract_data.get("party_a_label", "").upper()
        label_b = contract_data.get("party_b_label", "").upper()
        check   = represents or your_side
        # If our role word appears in label_b -> we are PARTY_B, and vice versa
        for role in _PARTY_B_ROLES:
            if role in label_b and role in check:
                print(f"[contract_parser] AI side inferred by contract label match: {role} in party_b_label -> PARTY_B")
                return "PARTY_B"
        for role in _PARTY_A_ROLES:
            if role in label_a and role in check:
                print(f"[contract_parser] AI side inferred by contract label match: {role} in party_a_label -> PARTY_A")
                return "PARTY_A"

    # 4. Default
    print(
        "[contract_parser] WARNING: Could not infer AI side from persona or scenario. "
        "Defaulting to PARTY_B. Set persona['represents'] to suppress this warning."
    )
    return "PARTY_B"


# ── Augmentation ───────────────────────────────────────────────────────────────

def _map_party(favours: str, ai_is: str) -> str:
    if favours == "NEUTRAL":
        return "NEUTRAL"
    return "AI" if favours == ai_is else "USER"


def augment_scenario(contract_data: dict, scenario: dict, ai_side: str) -> dict:
    """
    Merge contract terms into scenario dict.
    Agreed terms -> agreed_points. Contested terms -> contested_points (deduped).
    Returns augmented copy; does not mutate the original.
    """
    scenario = json.loads(json.dumps(scenario))

    if contract_data.get("contract_title"):
        scenario["contract_source"] = contract_data["contract_title"]

    existing_agreed    = set(scenario.get("agreed_points", []))
    existing_contested = {cp["issue"].lower() for cp in scenario.get("contested_points", [])}

    ai_label   = contract_data.get("party_b_label" if ai_side == "PARTY_B" else "party_a_label", "Seller")
    user_label = contract_data.get("party_a_label" if ai_side == "PARTY_B" else "party_b_label", "Buyer")

    for term in contract_data.get("agreed_terms", []):
        clause = term.get("clause", "")
        desc   = term.get("description", "")
        label  = f"[Contract {clause}] {desc}" if clause else desc
        if label not in existing_agreed:
            scenario.setdefault("agreed_points", []).append(label)

    for term in contract_data.get("contested_terms", []):
        issue = term.get("issue", "Unknown issue")
        if issue.lower() in existing_contested:
            continue

        if ai_side == "PARTY_B":
            ai_pos, user_pos = term.get("party_b_concern", ""), term.get("party_a_concern", "")
        else:
            ai_pos, user_pos = term.get("party_a_concern", ""), term.get("party_b_concern", "")

        scenario.setdefault("contested_points", []).append({
            "issue":                          issue,
            "clause":                         term.get("clause", ""),
            "current_drafting":               term.get("current_drafting", ""),
            f"{user_label.lower()}_position": user_pos,
            f"{ai_label.lower()}_position":   ai_pos,
            "favours":                        _map_party(term.get("favours", "NEUTRAL"), ai_side),
            "notes":                          term.get("notes", ""),
        })

    return scenario


def augment_persona(contract_data: dict, persona: dict, ai_side: str) -> dict:
    """
    Enrich persona wants/fears/redlines from contract analysis.
    AI strengths -> wants. AI risks -> fears. User strengths -> redlines.
    Returns augmented copy; does not mutate the original.
    """
    persona = json.loads(json.dumps(persona))

    sk  = "party_b_strengths" if ai_side == "PARTY_B" else "party_a_strengths"
    rk  = "party_b_risks"     if ai_side == "PARTY_B" else "party_a_risks"
    usk = "party_a_strengths" if ai_side == "PARTY_B" else "party_b_strengths"

    for s in contract_data.get(sk, []):
        e = f"[From contract] Preserve: {s}"
        if e not in persona.get("wants", []):
            persona.setdefault("wants", []).append(e)

    for r in contract_data.get(rk, []):
        e = f"[From contract] Risk exposure: {r}"
        if e not in persona.get("fears", []):
            persona.setdefault("fears", []).append(e)

    for s in contract_data.get(usk, []):
        e = f"[From contract] Do not concede without significant trade: {s}"
        if e not in persona.get("redlines", []):
            persona.setdefault("redlines", []).append(e)

    persona["contract_augmentation"] = {
        "contract_title": contract_data.get("contract_title", ""),
        "party_a_label":  contract_data.get("party_a_label", ""),
        "party_b_label":  contract_data.get("party_b_label", ""),
        "ai_plays":       ai_side,
    }
    return persona


# ── Public API ─────────────────────────────────────────────────────────────────

def parse_contract(config: dict, pdf_path: str, output_dir: str = None) -> dict:
    """
    Full pipeline: PDF file -> structured contract data dict.

    Args:
        config:     loaded config.json dict
        pdf_path:   path to the contract PDF
        output_dir: if provided, saves the extraction to
                    <output_dir>/contract_extraction_<timestamp>.json
                    so it can be read by the evaluator later.

    Returns:
        contract_data dict with keys: contract_title, party_a_label,
        party_b_label, agreed_terms, contested_terms, party_a/b_risks/strengths
    """
    import os
    from datetime import datetime

    print(f"[contract_parser] Reading: {pdf_path}")
    text = extract_pdf_text(pdf_path)
    print(f"[contract_parser] {len(text):,} chars, {text.count('[Page ')} pages.")
    print("[contract_parser] Analysing with LLM...")
    data = extract_contract_terms(config, text)
    print(
        f"[contract_parser] {len(data.get('agreed_terms', []))} agreed, "
        f"{len(data.get('contested_terms', []))} contested terms found."
    )

    # Save extraction to disk so the evaluator (and humans) can inspect it
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path  = os.path.join(output_dir, f"contract_extraction_{timestamp}.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        print(f"[contract_parser] Extraction saved: {out_path}")
        data["_extraction_path"] = out_path  # carry path forward for evaluator

    return data


def augment_inputs(
    contract_data: dict,
    scenario: dict,
    persona: dict,
    ai_side: str = None,
) -> tuple:
    """
    Augment both scenario and persona in one call.

    ai_side is inferred automatically from persona["represents"] and
    scenario["your_side"] unless you pass an explicit override
    ("PARTY_A" or "PARTY_B").

    Returns (augmented_scenario, augmented_persona).
    """
    if ai_side is None:
        ai_side = infer_ai_side(persona, scenario, contract_data)
    else:
        print(f"[contract_parser] AI side overridden manually: {ai_side}")

    return (
        augment_scenario(contract_data, scenario, ai_side),
        augment_persona(contract_data, persona, ai_side),
    )


# ── CLI ────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Parse a contract PDF and augment scenario/persona JSONs.")
    parser.add_argument("--pdf",      required=True,                  help="Path to contract PDF")
    parser.add_argument("--config",   default="config.json",          help="Path to config.json")
    parser.add_argument("--scenario", default="inputs/scenario.json", help="Path to scenario.json")
    parser.add_argument("--persona",  default="inputs/persona.json",  help="Path to persona.json")
    parser.add_argument(
        "--ai-side", default=None, choices=["PARTY_A", "PARTY_B"],
        help=(
            "Override which contract party the AI plays. "
            "If omitted, inferred automatically from persona['represents'] "
            "and scenario['your_side']."
        ),
    )
    parser.add_argument("--save",       action="store_true", help="Overwrite JSONs in place")
    parser.add_argument("--dump",       action="store_true", help="Print raw extraction JSON to stdout")
    parser.add_argument("--output-dir", default=".",         help="Directory to save contract_extraction_*.json (default: current dir)")
    args = parser.parse_args()

    def load(path, label):
        if not os.path.exists(path):
            print(f"Error: {label} not found at '{path}'"); sys.exit(1)
        with open(path, encoding="utf-8") as f: return json.load(f)

    config   = load(args.config,   "config.json")
    scenario = load(args.scenario, "scenario.json")
    persona  = load(args.persona,  "persona.json")

    contract_data = parse_contract(config, args.pdf, output_dir=args.output_dir)

    if args.dump:
        print("\n── Raw extraction ──")
        print(json.dumps(contract_data, indent=2))

    new_scenario, new_persona = augment_inputs(
        contract_data, scenario, persona, ai_side=args.ai_side
    )

    if args.save:
        with open(args.scenario, "w", encoding="utf-8") as f: json.dump(new_scenario, f, indent=2)
        print(f"[contract_parser] Updated: {args.scenario}")
        with open(args.persona, "w", encoding="utf-8") as f:  json.dump(new_persona,  f, indent=2)
        print(f"[contract_parser] Updated: {args.persona}")
    else:
        print("\n── Augmented contested_points ──")
        print(json.dumps(new_scenario.get("contested_points", []), indent=2))
        print("\n── Augmented persona wants/fears/redlines ──")
        print(json.dumps({k: new_persona.get(k) for k in ("wants", "fears", "redlines")}, indent=2))
        print("\nRun with --save to write back to JSON files.")

if __name__ == "__main__":
    main()
