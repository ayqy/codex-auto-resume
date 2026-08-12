from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def test_health_monitor_stops_on_terminal_state_before_kickstart():
    script = (ROOT / "scripts" / "monitor_reset_credit_launch_agent.sh").read_text()
    terminal_check = script.index('"(completed|expired|disabled)"')
    kickstart = script.index("launchctl kickstart")
    assert terminal_check < kickstart
    assert "terminal-state; monitor-exit" in script
