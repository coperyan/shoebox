"""Command parsing for the Slack bot: what chat input reaches the CLI."""

from shoebox.services.slack_bot_service import parse_command


class TestParseCommand:
    def test_unknown_command_is_rejected(self):
        assert parse_command("rm-rf", ["--force"]) is None

    def test_boolean_flags_pass_through(self):
        cmd, args = parse_command("watch-searches", ["--force", "--dry-run"])
        assert cmd == "watch-searches"
        assert args == ["--force", "--dry-run"]

    def test_unrecognized_flags_are_stripped(self):
        _, args = parse_command("watch-searches", ["--force", "--config", "/etc/passwd"])
        assert args == ["--force"]

    def test_reseed_keeps_its_value(self):
        _, args = parse_command("watch-searches", ["--reseed", "jordan-rookies"])
        assert args == ["--reseed", "jordan-rookies"]

    def test_reseed_equals_form(self):
        _, args = parse_command("watch-searches", ["--reseed=jordan-rookies"])
        assert args == ["--reseed", "jordan-rookies"]

    def test_reseed_is_repeatable(self):
        _, args = parse_command("watch-searches", ["--reseed", "s1", "--reseed=s2", "--force"])
        assert args == ["--reseed", "s1", "--reseed", "s2", "--force"]

    def test_only_keeps_its_value(self):
        _, args = parse_command("watch-searches", ["--only", "s1"])
        assert args == ["--only", "s1"]

    def test_valueless_reseed_is_dropped_whole(self):
        """A bare --reseed would make argparse abort the whole run; drop it."""
        _, args = parse_command("watch-searches", ["--reseed", "--force"])
        assert args == ["--force"]

    def test_valueless_reseed_at_end_is_dropped(self):
        _, args = parse_command("watch-searches", ["--force", "--reseed"])
        assert args == ["--force"]

    def test_value_on_boolean_flag_is_dropped(self):
        _, args = parse_command("watch-searches", ["--force=yes", "--dry-run"])
        assert args == ["--dry-run"]

    def test_value_never_leaks_as_bare_arg(self):
        """The value of a stripped unknown flag must not survive on its own."""
        _, args = parse_command("watch-searches", ["--config", "other.yaml", "--force"])
        assert "other.yaml" not in args
