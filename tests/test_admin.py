"""Tests for parse_admin_command in runtime/admin.py."""

import pytest

from runtime.admin import parse_admin_command


class TestEmptyInput:
    """Empty or blank input returns None."""

    def test_none_input(self):
        assert parse_admin_command(None) is None

    def test_empty_string(self):
        assert parse_admin_command("") is None

    def test_whitespace_only(self):
        assert parse_admin_command("   ") is None


class TestRequirePrefix:
    """When require_prefix=True, commands without a prefix return None."""

    def test_bare_command_rejected(self):
        assert parse_admin_command("help", require_prefix=True) is None

    def test_bare_system_status_rejected(self):
        assert parse_admin_command("system status", require_prefix=True) is None

    def test_elixir_do_prefix_accepted(self):
        result = parse_admin_command("elixir do help", require_prefix=True)
        assert result is not None
        assert result["key"] == "help"

    def test_do_prefix_accepted(self):
        result = parse_admin_command("do help", require_prefix=True)
        assert result is not None
        assert result["key"] == "help"


class TestHelpCommand:
    """'elixir do help' returns the help command."""

    def test_elixir_do_help(self):
        result = parse_admin_command("elixir do help")
        assert result is not None
        assert result["key"] == "help"
        assert result["kind"] == "command"
        assert result["command"] == "help"

    def test_do_help(self):
        result = parse_admin_command("do help")
        assert result is not None
        assert result["key"] == "help"

    def test_bare_help(self):
        result = parse_admin_command("help")
        assert result is not None
        assert result["key"] == "help"


class TestClanStatus:
    """'do clan status' returns clan.status command."""

    def test_do_clan_status(self):
        result = parse_admin_command("do clan status")
        assert result is not None
        assert result["key"] == "clan.status"
        assert result["resource"] == "clan"
        assert result["action"] == "status"
        assert result["path"] == ("clan", "status")

    def test_clan_status_short_flag(self):
        result = parse_admin_command("do clan status --short")
        assert result is not None
        assert result["key"] == "clan.status"
        assert result["short"] is True
        assert result["args"]["short"] == "true"

    def test_clan_status_no_short(self):
        result = parse_admin_command("do clan status")
        assert result["short"] is False
        assert result["args"]["short"] == "false"


class TestSystemStatus:
    """'do system status' returns system.status."""

    def test_do_system_status(self):
        result = parse_admin_command("do system status")
        assert result is not None
        assert result["key"] == "system.status"
        assert result["resource"] == "system"
        assert result["action"] == "status"

    def test_system_storage(self):
        result = parse_admin_command("do system storage")
        assert result is not None
        assert result["key"] == "system.storage"
        assert result["args"]["view"] == "all"

    def test_system_storage_with_view(self):
        result = parse_admin_command("do system storage clan")
        assert result is not None
        assert result["key"] == "system.storage"
        assert result["args"]["view"] == "clan"

    def test_system_schedule(self):
        result = parse_admin_command("do system schedule")
        assert result is not None
        assert result["key"] == "system.schedule"


class TestMemberShow:
    """'do member show SomePlayer' returns member.show with args."""

    def test_member_show_single_name(self):
        result = parse_admin_command("do member show SomePlayer")
        assert result is not None
        assert result["key"] == "member.show"
        assert result["args"]["member"] == "SomePlayer"

    def test_member_show_multi_word_name(self):
        result = parse_admin_command('do member show "King Thing"')
        assert result is not None
        assert result["key"] == "member.show"
        assert result["args"]["member"] == "King Thing"

    def test_member_show_no_name_returns_none(self):
        # member show with no member arg doesn't match (len(tail) < 2)
        result = parse_admin_command("do member show")
        assert result is None


class TestPreviewFlag:
    """Preview flag extraction."""

    def test_preview_flag_on_system_status(self):
        result = parse_admin_command("do system status --preview")
        assert result is not None
        assert result["preview"] is True

    def test_preview_word_on_help(self):
        result = parse_admin_command("do help --preview")
        # 'help' with tail (--preview stripped, but filtered becomes ['help'])
        # After filtering --preview, filtered = ['help'], head='help', tail=[]
        assert result is not None
        assert result["key"] == "help"
        assert result["preview"] is True

    def test_no_preview_by_default(self):
        result = parse_admin_command("do system status")
        assert result is not None
        assert result["preview"] is False

    def test_preview_keyword_without_dashes(self):
        result = parse_admin_command("do system status preview")
        assert result is not None
        assert result["preview"] is True


class TestSignalShow:
    """'do signal show' returns signal.show with defaults."""

    def test_signal_show_defaults(self):
        result = parse_admin_command("do signal show")
        assert result is not None
        assert result["key"] == "signal.show"
        assert result["args"]["view"] == "all"
        assert result["args"]["limit"] == "10"

    def test_signal_show_with_view(self):
        result = parse_admin_command("do signal show recent")
        assert result is not None
        assert result["key"] == "signal.show"
        assert result["args"]["view"] == "recent"

    def test_signal_publish_pending(self):
        result = parse_admin_command("do signal publish-pending")
        assert result is not None
        assert result["key"] == "signal.publish-pending"


class TestInvalidCommands:
    """Unrecognized commands return None."""

    def test_nonsense_command(self):
        result = parse_admin_command("do xyzzy frobulate")
        assert result is None

    def test_only_prefix(self):
        result = parse_admin_command("do")
        assert result is None

    def test_elixir_do_only(self):
        result = parse_admin_command("elixir do")
        assert result is None

    def test_only_flags(self):
        result = parse_admin_command("do --preview --short")
        assert result is None


class TestMalformedShlex:
    """Malformed shlex input (unmatched quotes) returns None."""

    def test_unmatched_single_quote(self):
        assert parse_admin_command("do member show 'unmatched") is None

    def test_unmatched_double_quote(self):
        assert parse_admin_command('do member show "unmatched') is None


class TestAdditionalCommands:
    """Verify other command paths parse correctly."""

    def test_clan_war(self):
        result = parse_admin_command("do clan war")
        assert result is not None
        assert result["key"] == "clan.war"

    def test_clan_members(self):
        result = parse_admin_command("do clan members")
        assert result is not None
        assert result["key"] == "clan.members"
        assert result["args"]["detail"] == "summary"

    def test_clan_members_full(self):
        result = parse_admin_command("do clan members full")
        assert result is not None
        assert result["args"]["detail"] == "full"

    def test_activity_list(self):
        result = parse_admin_command("do activity list")
        assert result is not None
        assert result["key"] == "activity.list"

    def test_integration_list(self):
        result = parse_admin_command("do integration list")
        assert result is not None
        assert result["key"] == "integration.list"

    def test_integration_poap_kings_status(self):
        result = parse_admin_command("do integration poap-kings status")
        assert result is not None
        assert result["key"] == "integration.poap-kings.status"

    def test_memory_show(self):
        result = parse_admin_command("do memory show")
        assert result is not None
        assert result["key"] == "memory.show"

    def test_member_set(self):
        result = parse_admin_command("do member set Ditika discord some-value")
        assert result is not None
        assert result["key"] == "member.set"
        assert result["args"]["member"] == "Ditika"
        assert result["args"]["field"] == "discord"
        assert result["args"]["value"] == "some-value"

    def test_member_clear(self):
        result = parse_admin_command("do member clear Ditika discord")
        assert result is not None
        assert result["key"] == "member.clear"
        assert result["args"]["member"] == "Ditika"
        assert result["args"]["field"] == "discord"


class TestPrefixVariants:
    """Different prefix forms are recognized."""

    def test_run_prefix(self):
        result = parse_admin_command("run help")
        assert result is not None
        assert result["key"] == "help"

    def test_elixir_do_prefix(self):
        result = parse_admin_command("elixir-do help")
        assert result is not None
        assert result["key"] == "help"

    def test_case_insensitive_prefix(self):
        result = parse_admin_command("ELIXIR DO help")
        assert result is not None
        assert result["key"] == "help"
