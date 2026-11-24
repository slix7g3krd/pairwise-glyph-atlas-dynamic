import pytest
import graphics.glyph_atlas_dynamic as mod

CASES = [
    ("key_with_size", (1,)),
    ("slot_check", (1,)),
    ("frame_ref", (1,)),
    ("uv_rebuild", (1,)),
    ("sdf_norm", (1,)),
    ("codepoint_split", (1,)),
    ("determinism", (1,)),
    ("validation", (1,)),
    ("audit", (1,)),
    ("fallback", (1,)),
    ("rasterize", (1,)),
    ("metrics", (1,)),
    ("atlas_page", (1,)),
    ("layout", (1,)),
    ("draw", (1,)),
]

@pytest.mark.parametrize("fn_name,args", CASES)
def test_contract(fn_name, args):
    fn = getattr(mod, fn_name)
    assert fn(*args) is not None
