"""Check bundled prompts against the desktop defaults; --write regenerates them."""
import json
from pathlib import Path
import sys
import types

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.modules["storage"] = types.ModuleType("storage")
sys.modules.setdefault("requests", types.ModuleType("requests"))
import smart_prompt
import action_api

smart_prompt.load_examples = lambda: smart_prompt.DEFAULT_EXAMPLES
modes = ["smart_auto", "write_email", "make_todo_list", "translate", "summarize"]
expected = {mode: action_api.build_messages("{text}", mode, target_lang="{target}") for mode in modes}
path = ROOT / "ios/GusanKit/Sources/GusanKit/Resources/prompts.json"
if "--write" in sys.argv:
    path.write_text(json.dumps(expected, ensure_ascii=False, indent=2) + "\n")
else:
    assert json.loads(path.read_text()) == expected, "Desktop/iOS prompt drift"
    print("Prompt parity OK (all five modes, all default examples)")
