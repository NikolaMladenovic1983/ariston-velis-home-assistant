"""i18n tests: catalogs complete and consistent, generated files up to date, Jinja macro and Serbian
transliteration work, dashboard strategy builds in every language. Run: python -m pytest tests -q"""
import importlib.util
import pathlib
import shutil
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("i18n_build", ROOT / "tools/i18n_build.py")
ib = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ib)


def test_catalogs_complete_and_placeholders_match():
    assert ib.validate(ib.load()) == []


def test_generated_files_are_up_to_date():
    stale = [p for p, text in ib.outputs(ib.load()).items()
             if not p.exists() or p.read_text(encoding="utf-8") != text]
    assert stale == [], f"run python tools/i18n_build.py: {stale}"


def test_seven_languages_plus_cyrillic():
    cats = ib.load()
    assert sorted(cats) == ["de", "en", "es", "fr", "it", "ru", "sr", "sr-Latn"]
    for lang in cats:
        assert (ROOT / f"ha-config/custom_components/ariston_schedule/translations/{lang}.json").exists()


def test_serbian_transliteration():
    t = ib.to_cyrillic
    assert t("Ljubljana, njiva, džep, Đorđe") == "Љубљана, њива, џеп, Ђорђе"
    assert t("Voda {temp} °C, 1–3 h, 24h, kWh, Excel, email") == "Вода {temp} °C, 1–3 h, 24h, kWh, Excel, email"
    assert t('[{"days": "radni"}] /config/ariston_schedule_backups') == '[{"days": "radni"}] /config/ariston_schedule_backups'


def _render(lang_name: str, expr: str) -> str:
    jinja2 = pytest.importorskip("jinja2")
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(ROOT / "ha-config/custom_templates")))
    env.globals["states"] = lambda entity_id: lang_name
    return env.from_string("{% from 'bojler_i18n.jinja' import t %}" + expr).render().strip()


def test_jinja_macro_languages_and_placeholders():
    assert _render("Deutsch", "{{ t('ready_msg', {'temp': 55, 'showers': 2}) }}") == "Heizen beendet: 55 °C, 2 Dusche(n)."
    assert _render("Српски", "{{ t('boost_on') }}") == "BOOST укључен."
    assert _render("Srpski", "{{ t('mode_' ~ 'MANUAL') }}") == "ručno"
    assert _render("unknown", "{{ t('sched_title') }}") == "Boiler – schedule"  # fallback English


def test_dashboard_strategy_node():
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    r = subprocess.run([node, str(ROOT / "tests/test_dashboard.mjs")], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr or r.stdout


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
