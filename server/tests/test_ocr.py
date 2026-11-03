import pytest

from aqserver.ocr import sevenseg
from aqserver.ocr.pipeline import DeviceSpec, FieldSpec, judge, run_frame, warp_size
from render import SCENE_CORNERS, add_noise, render_display, render_scene

# Numbers chosen to cover every digit, narrow digits without left-hand segments (3, 7),
# runs of 1s, a minus sign and a decimal point.
CASES = ["1234", "88", "7", "105", "0", "1111", "-12", "4096", "96", "561", "3", "33", "77", "73", "37",
         "71", "17", "111", "707", "1337", "23.5"]


@pytest.mark.parametrize("text", CASES)
@pytest.mark.parametrize("aspect", [0.5, 0.55, 0.62])  # digit width / height of the font
@pytest.mark.parametrize("slant", [0, 8])
@pytest.mark.parametrize("digit_h", [24, 40, 80])
@pytest.mark.parametrize("ghost", [False, True])
def test_sevenseg_reads_rendered_digits(text, aspect, slant, digit_h, ghost):
    img = add_noise(render_display(text, cells=4, digit_h=digit_h, slant_deg=slant, ghost=ghost, aspect=aspect),
                    seed=digit_h)
    out = sevenseg.read(img, {"slant": slant, "decimals": "auto" if "." in text else 0})
    assert out.text == text
    # Correct readings should be confident enough to be accepted without review.
    assert out.confidence >= 0.6


def test_implied_decimals():
    img = add_noise(render_display("235", cells=4, digit_h=40))
    assert sevenseg.read(img, {"decimals": 1}).text == "23.5"
    img = add_noise(render_display("5", cells=4, digit_h=40))
    assert sevenseg.read(img, {"decimals": 2}).text == "0.05"


def test_light_digits_on_dark_background():
    img = add_noise(render_display("418", cells=3, digit_h=50, fg=(250, 250, 250), bg=(90, 40, 20)))
    assert sevenseg.read(img, {}).text == "418"


def test_blank_display_is_not_a_number():
    img = add_noise(render_display("", cells=3, digit_h=50), sigma=2)
    out = sevenseg.read(img, {})
    assert out.text == "" and out.confidence == 0


def test_full_pipeline_with_perspective():
    frame, boxes = render_scene("37", "812")
    w, h = warp_size(SCENE_CORNERS)
    dev = DeviceSpec(id=1, corners=SCENE_CORNERS, warp_w=w, warp_h=h, fields=[
        FieldSpec(id=1, metric="pm25", box=boxes["pm25"], min_value=0, max_value=1000),
        FieldSpec(id=2, metric="co2", box=boxes["co2"], min_value=300, max_value=10000),
    ])
    results = {r.metric: r for r in run_frame(frame, [dev], review_confidence=0.6)}
    assert results["pm25"].value == 37 and results["pm25"].status == "ok"
    # 812 ppm is plausible; read correctly and accepted
    assert results["co2"].value == 812 and results["co2"].status == "ok"


def test_judge_flags_out_of_range_and_low_confidence():
    assert judge(5000, 0.9, None, 0, 1000, 0.6)[0] == "review"
    assert judge(50, 0.3, None, 0, 1000, 0.6)[0] == "review"
    assert judge(None, 0.9, "blank display", 0, 1000, 0.6) == ("review", "blank display")
    assert judge(50, 0.9, None, 0, 1000, 0.6) == ("ok", None)
