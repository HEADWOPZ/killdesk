from pathlib import Path

from typer.testing import CliRunner

from killdesk.main import app

FIXTURES = Path(__file__).parent / "fixtures" / "cycle"
runner = CliRunner()


def test_no_shadow_is_refused() -> None:
    result = runner.invoke(app, ["run", "--once", "--mock", "--no-shadow"])
    assert result.exit_code == 2
    assert "shadow-only" in result.stderr + result.stdout


def test_mock_once_against_fixtures_then_report(tmp_path: Path) -> None:
    db = tmp_path / "desk.db"
    result = runner.invoke(
        app,
        [
            "run",
            "--once",
            "--mock",
            "--fixtures",
            str(FIXTURES),
            "--db",
            str(db),
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "shadow OPEN GOOD" in result.stdout
    assert "no order sent" in result.stdout
    assert db.exists()
    report = runner.invoke(app, ["report", "--db", str(db)])
    assert report.exit_code == 0, report.stdout
    assert "shadow P&L" in report.stdout
    assert "hit rate" in report.stdout
    assert "too_early" in report.stdout
    assert "honeypot" in report.stdout
