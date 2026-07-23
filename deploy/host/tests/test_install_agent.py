"""Regression tests for the root installer's pure and staged operations."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[3]
INSTALLER = ROOT / "deploy" / "host" / "install-agent.sh"
BASH = shutil.which("bash") or "/bin/bash"


def run_bash(
    body: str, *, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    command = f"source {INSTALLER!s}; init_defaults; {body}"
    return subprocess.run(
        [BASH, "-c", command],
        text=True,
        capture_output=True,
        env={**os.environ, **(env or {})},
        check=False,
    )


class InstallerValidationTests(unittest.TestCase):
    def assert_ok(self, body: str) -> str:
        result = run_bash(body)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def assert_bad(self, body: str, message: str) -> None:
        result = run_bash(body)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(message, result.stderr)

    def test_option_without_value_has_clear_error(self) -> None:
        result = subprocess.run(
            [BASH, str(INSTALLER), "--agent-ip"],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Error: --agent-ip requires a value.", result.stderr)
        self.assertNotIn("unbound variable", result.stderr)

    def test_invalid_port_memory_dns_and_newline_injection(self) -> None:
        self.assert_bad("is_valid_port 0 || die invalid-port", "invalid-port")
        self.assert_bad("is_valid_port 65536 || die invalid-port", "invalid-port")
        self.assert_bad("is_valid_port '8 765' || die invalid-port", "invalid-port")
        self.assert_bad(
            "is_valid_memory_limit 1GB || die invalid-memory", "invalid-memory"
        )
        self.assert_bad("is_valid_dns '-bad.example' || die invalid-dns", "invalid-dns")
        self.assert_bad(
            "is_valid_dns $'good.example\\nsubjectAltName=DNS:evil' || die newline",
            "newline",
        )

    def test_ipv4_ipv6_and_cidr_rejection(self) -> None:
        self.assert_ok("is_valid_ip 192.0.2.10; is_valid_ip 2001:db8::10")
        self.assert_bad("is_valid_ip 192.0.2.0/24 || die invalid-ip", "invalid-ip")
        self.assert_bad("is_valid_ip '192.0.2.1;id' || die invalid-ip", "invalid-ip")

    def test_single_and_multi_nic_selection(self) -> None:
        output = self.assert_ok("select_agent_ip '' 0 192.0.2.10")
        self.assertEqual(output.strip(), "192.0.2.10")
        self.assert_bad(
            "select_agent_ip '' 0 192.0.2.10 2001:db8::10",
            "Multiple candidate IPs",
        )
        self.assert_bad(
            "select_agent_ip 192.0.2.99 0 192.0.2.10",
            "not assigned",
        )
        output = self.assert_ok("select_agent_ip 192.0.2.99 1 192.0.2.10")
        self.assertEqual(output.strip(), "192.0.2.99")

    def test_path_storage_and_remote_validation(self) -> None:
        self.assert_bad("canonicalize_path --dump-root relative || true; false", "")
        self.assert_ok("is_valid_identifier backup-hdd; is_valid_identifier gdrive")
        self.assert_bad(
            "is_valid_identifier 'bad remote' || die invalid-name", "invalid-name"
        )
        self.assert_bad(
            "is_valid_identifier '../remote' || die invalid-name", "invalid-name"
        )

    def test_firewall_option_semantics_and_conflicts(self) -> None:
        self.assert_ok("parse_args --skip-firewall; [[ $FIREWALL_MODE == unchanged ]]")
        self.assert_ok("parse_args --remove-firewall; [[ $FIREWALL_MODE == removed ]]")
        self.assert_ok(
            "parse_args --configure-firewall; [[ $FIREWALL_MODE == managed ]]"
        )
        self.assert_bad(
            "parse_args --skip-firewall --remove-firewall",
            "conflicts with --skip-firewall",
        )


class FirewallTests(unittest.TestCase):
    def test_render_is_idempotent_and_updates_stale_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "first.nft"
            second = Path(directory) / "second.nft"
            changed = Path(directory) / "changed.nft"
            result = run_bash(
                f"render_firewall {first} 192.0.2.20 8765; "
                f"render_firewall {second} 192.0.2.20 8765; "
                f"cmp {first} {second}; "
                f"render_firewall {changed} 192.0.2.21 9443"
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            old = first.read_text()
            new = changed.read_text()
            self.assertIn('iifname "lo"', new)
            self.assertIn("192.0.2.21", new)
            self.assertIn("9443", new)
            self.assertNotIn("192.0.2.20", new)
            self.assertNotIn("8765", new)
            self.assertNotIn("hook output", new)
            self.assertEqual(old.count("chain agent_input"), 1)

    def test_ipv6_firewall_and_persistent_loader(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            rules = Path(directory) / "rules.nft"
            loader = Path(directory) / "loader"
            unit = Path(directory) / "unit"
            result = run_bash(
                f"render_firewall {rules} 2001:db8::20 8765; "
                f"render_firewall_loader {loader}; render_firewall_unit {unit}"
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("ip6 saddr 2001:db8::20", rules.read_text())
            self.assertIn("nft --check --file", loader.read_text())
            self.assertIn(
                "Before=network-pre.target proxsync-agent.service", unit.read_text()
            )
            self.assertIn("RemainAfterExit=yes", unit.read_text())

    def test_nft_syntax_failure_is_fatal_before_apply(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            mock = Path(directory) / "nft"
            mock.write_text(
                "#!/usr/bin/env bash\n"
                '[[ "$1" == "list" ]] && exit 1\n'
                '[[ "$1" == "--check" ]] && exit 42\n'
                "exit 0\n"
            )
            mock.chmod(0o755)
            rules = Path(directory) / "rules.nft"
            transaction = Path(directory) / "transaction.nft"
            result = run_bash(
                f"render_firewall {rules} 192.0.2.20 8765; "
                f"validate_firewall_rules {rules} {transaction}",
                env={"PATH": f"{directory}:{os.environ['PATH']}"},
            )
            self.assertEqual(result.returncode, 42)

    def test_snapshot_restores_environment_and_certificate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config"
            config.mkdir()
            env_file = config / "agent.env"
            certificate = config / "server.crt"
            env_file.write_text("old-env\n")
            certificate.write_text("old-cert\n")
            backup = root / "backup"
            backup.mkdir()
            result = run_bash(
                f"BACKUP_DIR={backup}; snapshot_path {config} config; "
                f"printf changed > {env_file}; printf changed > {certificate}; "
                f"restore_path {config} config"
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(env_file.read_text(), "old-env\n")
            self.assertEqual(certificate.read_text(), "old-cert\n")

    def test_service_restart_failure_triggers_transaction_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as directory_name:
            root = Path(directory_name)
            install = root / "install"
            config = root / "config"
            install.mkdir()
            config.mkdir()
            (install / "app").write_text("old-app\n")
            (config / "agent.env").write_text("old-env\n")
            unit = root / "agent.service"
            unit.write_text("old-unit\n")
            work = root / "work"
            work.mkdir()
            body = (
                "systemctl() { "
                'case "$1" in is-active|is-enabled|restart) return 0 ;; *) return 0 ;; esac; }; '
                "nft() { return 1; }; "
                f"INSTALL_DIR={install}; CONFIG_DIR={config}; UNIT_FILE={unit}; "
                f"FIREWALL_FILE={root / 'firewall.nft'}; "
                f"FIREWALL_LOADER={root / 'loader'}; FIREWALL_UNIT={root / 'firewall.service'}; "
                f"RCLONE_CONFIG={root / 'rclone.conf'}; "
                f"WORK_DIR={work}; begin_transaction; trap rollback_on_error ERR; "
                f"printf new-app > {install / 'app'}; printf new-env > {config / 'agent.env'}; "
                "false"
            )
            result = run_bash(body)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("restoring the previous ProxSync state", result.stderr)
            self.assertEqual((install / "app").read_text(), "old-app\n")
            self.assertEqual((config / "agent.env").read_text(), "old-env\n")

    def test_health_endpoint_failure_is_fatal(self) -> None:
        with tempfile.TemporaryDirectory() as directory_name:
            env_file = Path(directory_name) / "agent.env"
            env_file.write_text("safe=true\n")
            result = run_bash(
                "systemctl() { return 0; }; "
                "leaf_pair_valid() { return 0; }; "
                "certificate_has_desired_sans() { return 0; }; "
                "verify_firewall() { return 0; }; "
                "python3() { return 1; }; "
                f"ENV_FILE={env_file}; TLS_DIR={directory_name}; "
                "AGENT_DNS=agent.example; AGENT_IP=192.0.2.10; PORT=8765; "
                "verify_installation"
            )
            self.assertNotEqual(result.returncode, 0)


@unittest.skipUnless(shutil.which("openssl"), "OpenSSL is required")
class PkiTests(unittest.TestCase):
    def prepare(
        self, directory: Path, dns: str = "agent.example", ip: str = "192.0.2.10"
    ) -> None:
        result = run_bash(
            f"mkdir -p {directory}; prepare_pki {directory} {dns} {ip} 0 0 0"
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_full_san_set_and_dns_or_ip_change_rotates_server_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory_name:
            directory = Path(directory_name)
            self.prepare(directory)
            ca_before = (directory / "ca.crt").read_bytes()
            dashboard_before = (directory / "dashboard.crt").read_bytes()
            server_before = (directory / "server.crt").read_bytes()
            result = run_bash(
                f"prepare_pki {directory} renamed.example 2001:db8::10 0 0 0; "
                f"certificate_has_desired_sans {directory / 'server.crt'} "
                "renamed.example 2001:db8::10"
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((directory / "ca.crt").read_bytes(), ca_before)
            self.assertEqual(
                (directory / "dashboard.crt").read_bytes(), dashboard_before
            )
            self.assertNotEqual((directory / "server.crt").read_bytes(), server_before)

    def test_missing_server_and_dashboard_keys_are_repaired(self) -> None:
        with tempfile.TemporaryDirectory() as directory_name:
            directory = Path(directory_name)
            self.prepare(directory)
            ca_before = (directory / "ca.crt").read_bytes()
            (directory / "server.key").unlink()
            (directory / "dashboard.key").unlink()
            result = run_bash(
                f"prepare_pki {directory} agent.example 192.0.2.10 0 1 0; "
                f"leaf_pair_valid {directory / 'ca.crt'} {directory / 'server.crt'} "
                f"{directory / 'server.key'}; "
                f"leaf_pair_valid {directory / 'ca.crt'} {directory / 'dashboard.crt'} "
                f"{directory / 'dashboard.key'}"
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((directory / "ca.crt").read_bytes(), ca_before)

    def test_missing_ca_key_blocks_required_rotation(self) -> None:
        with tempfile.TemporaryDirectory() as directory_name:
            directory = Path(directory_name)
            self.prepare(directory)
            (directory / "ca.key").unlink()
            result = run_bash(
                f"prepare_pki {directory} changed.example 192.0.2.10 0 0 0"
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("CA key is missing", result.stderr)

    def test_corrupted_certificate_is_repaired_with_existing_ca(self) -> None:
        with tempfile.TemporaryDirectory() as directory_name:
            directory = Path(directory_name)
            self.prepare(directory)
            ca_before = (directory / "ca.crt").read_bytes()
            (directory / "server.crt").write_text("not a certificate")
            result = run_bash(
                f"prepare_pki {directory} agent.example 192.0.2.10 0 1 0; "
                f"valid_certificate {directory / 'server.crt'}"
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((directory / "ca.crt").read_bytes(), ca_before)

    def test_hmac_is_rendered_without_leaking_into_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory_name:
            destination = Path(directory_name) / "agent.env"
            secret = "0123456789abcdef" * 4
            result = run_bash(
                f"AGENT_IP=192.0.2.10; PORT=8765; DUMP_ROOT=/dump; TEMP_DIR=/tmp; "
                f"BACKUP_STORAGE=backup; RCLONE_CONFIG=/rclone.conf; "
                f"render_environment {destination} {secret} 192.0.2.20/32"
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn(secret, result.stdout + result.stderr)
            self.assertIn(secret, destination.read_text())


class RcloneTests(unittest.TestCase):
    def make_mock(self, directory: Path) -> Path:
        mock = directory / "rclone"
        mock.write_text(
            "#!/usr/bin/env bash\n"
            'case "$*" in\n'
            '  "version") exit 0 ;;\n'
            '  *"listremotes"*) [[ "${RCLONE_MODE:-ok}" == missing ]] || printf "gdrive:\\n" ;;\n'
            '  *" lsd "*) [[ "${RCLONE_MODE:-ok}" == fail ]] && '
            'printf "refresh_token=super-secret-token\\n" >&2 && exit 9; exit 0 ;;\n'
            "esac\n"
        )
        mock.chmod(0o755)
        timeout = directory / "timeout"
        timeout.write_text('#!/usr/bin/env bash\nshift\nexec "$@"\n')
        timeout.chmod(0o755)
        return mock

    def test_config_unreadable_or_remote_missing_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory_name:
            directory = Path(directory_name)
            self.make_mock(directory)
            result = run_bash(
                f"WORK_DIR={directory}; RCLONE_CHECK_CONFIG={directory / 'missing'}; "
                "RCLONE_REMOTE=gdrive; check_rclone 1 0",
                env={"PATH": f"{directory}:{os.environ['PATH']}"},
            )
            self.assertNotEqual(result.returncode, 0)
            config = directory / "rclone.conf"
            config.write_text("[gdrive]\ntype = drive\n")
            result = run_bash(
                f"WORK_DIR={directory}; RCLONE_CHECK_CONFIG={config}; RCLONE_REMOTE=other; "
                "check_rclone 1 0",
                env={"PATH": f"{directory}:{os.environ['PATH']}"},
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("was not found", result.stderr)

    def test_authenticated_connectivity_and_secret_redaction(self) -> None:
        with tempfile.TemporaryDirectory() as directory_name:
            directory = Path(directory_name)
            self.make_mock(directory)
            config = directory / "rclone.conf"
            config.write_text("[gdrive]\ntype = drive\ntoken = super-secret-token\n")
            result = run_bash(
                f"WORK_DIR={directory}; RCLONE_CHECK_CONFIG={config}; RCLONE_REMOTE=gdrive; "
                "check_rclone 1 1",
                env={"PATH": f"{directory}:{os.environ['PATH']}"},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            result = run_bash(
                f"WORK_DIR={directory}; RCLONE_CHECK_CONFIG={config}; RCLONE_REMOTE=gdrive; "
                "check_rclone 1 1",
                env={
                    "PATH": f"{directory}:{os.environ['PATH']}",
                    "RCLONE_MODE": "fail",
                },
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn("super-secret-token", result.stdout + result.stderr)
            self.assertIn("details redacted", result.stderr)


if __name__ == "__main__":
    unittest.main()
