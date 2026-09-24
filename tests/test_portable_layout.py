import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "ensure_youtube_mcp.sh"
SETUP = ROOT / "scripts" / "setup_youtube_mcp.sh"
INSTALL_NAME = "youtube"
MCP_COMMIT = "06d5e7a83783f7a44498da88ade2ccaa42238747"


class PortableLayoutTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def write_executable(self, path, content):
        path.write_text(content, encoding="utf-8")
        path.chmod(0o755)

    def make_install(self, parent=None):
        install = (parent or self.root) / INSTALL_NAME
        for relative in (
            "app/dist",
            "runtime/bin",
            "state",
            "work",
        ):
            (install / relative).mkdir(parents=True, exist_ok=True)
        (install / "app/dist/stdio-server.js").write_text("", encoding="utf-8")
        (install / "README.md").write_text("# Test installation\n", encoding="utf-8")
        (install / "VERSION").write_text(
            "\n".join(
                (
                    "installation_name=youtube",
                    "youtube_mcp_version=1.2.0",
                    f"youtube_mcp_commit={MCP_COMMIT}",
                    "node_version=v24.14.0",
                    "platform=linux-x86_64",
                    "",
                )
            ),
            encoding="utf-8",
        )
        node = install / "runtime/bin/node"
        node.write_text(
            "#!/bin/sh\n"
            "if [ \"${1:-}\" = --version ]; then\n"
            "  echo v24.14.0\n"
            "else\n"
            "  echo '{\"tools\":[{\"name\":\"research-video\"}]}'\n"
            "fi\n",
            encoding="utf-8",
        )
        node.chmod(0o755)
        return install

    def run_installer(self):
        return subprocess.run(
            [
                "sh",
                str(INSTALLER),
                "--search-root",
                str(self.root),
                "--install-parent",
                str(self.root),
            ],
            check=False,
            capture_output=True,
            text=True,
            env=os.environ.copy(),
        )

    def make_fake_codex(self, install, *, mismatched=False):
        fake_bin = self.root / "codex-bin"
        fake_bin.mkdir(exist_ok=True)
        calls = self.root / "codex-calls.txt"
        state = self.root / "codex-registration"
        if mismatched:
            state.write_text("mismatched\n", encoding="utf-8")
        self.write_executable(
            fake_bin / "codex",
            "#!/bin/sh\n"
            "set -eu\n"
            "printf '%s\\n' \"$*\" >>\"$FAKE_CODEX_CALLS\"\n"
            "case \"$1 $2\" in\n"
            "  'mcp get')\n"
            "    [ -f \"$FAKE_CODEX_STATE\" ] || exit 1\n"
            "    if [ \"$(cat \"$FAKE_CODEX_STATE\")\" = mismatched ]; then\n"
            "      echo '  command: /different/node'\n"
            "      echo '  args: /different/server.js'\n"
            "    else\n"
            "      echo \"  command: $EXPECTED_NODE\"\n"
            "      echo \"  args: $EXPECTED_SERVER\"\n"
            "    fi\n"
            "    ;;\n"
            "  'mcp add') printf '%s\\n' registered >\"$FAKE_CODEX_STATE\" ;;\n"
            "  *) exit 2 ;;\n"
            "esac\n",
        )
        environment = os.environ.copy()
        environment.update(
            {
                "CODEX_MCP_CLI": str(fake_bin / "codex"),
                "YOUTUBE_SKILL_HOME": str(self.root),
                "FAKE_CODEX_CALLS": str(calls),
                "FAKE_CODEX_STATE": str(state),
                "EXPECTED_NODE": str(install / "runtime/bin/node"),
                "EXPECTED_SERVER": str(install / "app/dist/stdio-server.js"),
            }
        )
        return environment, calls

    def run_setup(self, environment):
        return subprocess.run(
            ["sh", str(SETUP)],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )

    def test_recognizes_only_the_exact_maintained_layout(self):
        install = self.make_install()
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), str(install))
        self.assertEqual(
            {path.name for path in install.iterdir()},
            {"app", "runtime", "state", "work", "README.md", "VERSION"},
        )

    def test_default_install_lives_under_codex_home_mcp_servers(self):
        codex_home = self.root / "codex-home"
        install_parent = codex_home / "mcp-servers"
        install = self.make_install(install_parent)
        environment = os.environ.copy()
        environment.pop("YOUTUBE_SKILL_HOME", None)
        environment["CODEX_HOME"] = str(codex_home)

        result = subprocess.run(
            ["sh", str(INSTALLER)],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), str(install))

    def test_missing_state_requires_explicit_replacement(self):
        install = self.make_install()
        (install / "state").rmdir()
        result = self.run_installer()
        self.assertEqual(result.returncode, 1)
        self.assertIn("Replacement required", result.stderr)
        self.assertFalse((install / "state").exists())
        self.assertTrue((install / "work").is_dir())

    def test_unexpected_root_directory_is_not_accepted(self):
        install = self.make_install()
        unexpected = install / "unexpected-root"
        unexpected.mkdir()
        result = self.run_installer()
        self.assertEqual(result.returncode, 1)
        self.assertIn("Replacement required", result.stderr)
        self.assertTrue(unexpected.is_dir())

    def test_portable_config_directory_is_not_accepted(self):
        install = self.make_install()
        config = install / "config"
        config.mkdir()
        result = self.run_installer()
        self.assertEqual(result.returncode, 1)
        self.assertIn("Replacement required", result.stderr)
        self.assertTrue(config.is_dir())

    def test_installer_contains_no_root_launcher_or_backup_generation(self):
        source = INSTALLER.read_text(encoding="utf-8")
        self.assertNotIn('"$portable/bin/', source)
        self.assertNotIn("${INSTALL_NAME}-invalid-", source)
        self.assertIn('"$portable/state"', source)
        self.assertIn('"$portable/work"', source)

    def test_build_uses_one_temporary_npm_cache_and_cleans_it(self):
        fake_bin = self.root / "fake-bin"
        fake_bin.mkdir()
        install_parent = self.root / "install-parent"
        install_parent.mkdir()
        calls = self.root / "npm-calls.txt"
        inherited_cache = self.root / "unusable-inherited-cache"
        inherited_cache.write_text("not a directory\n", encoding="utf-8")
        inherited_cache.chmod(0o000)

        self.write_executable(fake_bin / "git", "#!/bin/sh\nexit 0\n")
        self.write_executable(
            fake_bin / "node",
            "#!/bin/sh\n"
            "if [ \"${1:-}\" = --version ]; then\n"
            "  echo v24.14.0\n"
            "else\n"
            "  echo '{\"tools\":[{\"name\":\"research-video\"}]}'\n"
            "fi\n",
        )
        self.write_executable(
            fake_bin / "npm",
            "#!/bin/sh\n"
            "set -eu\n"
            "[ \"${NPM_CONFIG_CACHE:-}\" != \"$INHERITED_NPM_CACHE\" ] || exit 91\n"
            "case \"${NPM_CONFIG_CACHE:-}\" in\n"
            "  \"$FAKE_INSTALL_PARENT\"/.youtube-mcp-build.*/npm-cache) ;;\n"
            "  *) exit 92 ;;\n"
            "esac\n"
            "[ -d \"$NPM_CONFIG_CACHE\" ] || exit 93\n"
            ": >\"$NPM_CONFIG_CACHE/fake-npm-write\"\n"
            "if [ -s \"$FAKE_NPM_CALLS\" ]; then\n"
            "  IFS= read -r first_cache <\"$FAKE_NPM_CALLS\"\n"
            "  [ \"$first_cache\" = \"$NPM_CONFIG_CACHE\" ] || exit 94\n"
            "fi\n"
            "printf '%s\\n' \"$NPM_CONFIG_CACHE\" >>\"$FAKE_NPM_CALLS\"\n"
            "echo 'simulated npm progress on stdout'\n"
            "case \"$*\" in\n"
            "  'ci --no-audit --no-fund') ;;\n"
            "  'run build') mkdir -p dist; : >dist/stdio-server.js ;;\n"
            "  *) exit 95 ;;\n"
            "esac\n",
        )

        environment = os.environ.copy()
        environment.update(
            {
                "PATH": f"{fake_bin}{os.pathsep}{environment['PATH']}",
                "CODEX_PRIMARY_RUNTIME_NODE": str(fake_bin / "node"),
                "NPM_CONFIG_CACHE": str(inherited_cache),
                "INHERITED_NPM_CACHE": str(inherited_cache),
                "FAKE_INSTALL_PARENT": str(install_parent),
                "FAKE_NPM_CALLS": str(calls),
            }
        )
        result = subprocess.run(
            [
                "sh",
                str(INSTALLER),
                "--search-root",
                str(self.root / "empty-search-root"),
                "--install-parent",
                str(install_parent),
            ],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), str(install_parent / INSTALL_NAME))
        cache_paths = calls.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(cache_paths), 2)
        self.assertEqual(cache_paths[0], cache_paths[1])
        cache_path = Path(cache_paths[0])
        self.assertEqual(cache_path.name, "npm-cache")
        self.assertEqual(cache_path.parent.parent, install_parent)
        self.assertFalse(cache_path.exists())
        self.assertEqual(list(install_parent.glob(".youtube-mcp-build.*")), [])
        self.assertEqual(
            {path.name for path in (install_parent / INSTALL_NAME).iterdir()},
            {"app", "runtime", "state", "work", "README.md", "VERSION"},
        )
        self.assertFalse((install_parent / INSTALL_NAME / "npm-cache").exists())
        inherited_cache.chmod(0o600)
        self.assertEqual(
            inherited_cache.read_text(encoding="utf-8"),
            "not a directory\n",
        )

    def test_setup_registers_once_and_is_idempotent(self):
        install = self.make_install()
        environment, calls = self.make_fake_codex(install)

        first = self.run_setup(environment)
        second = self.run_setup(environment)

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("already registered", second.stdout)
        recorded = calls.read_text(encoding="utf-8").splitlines()
        self.assertEqual(sum(line.startswith("mcp add youtube") for line in recorded), 1)

    def test_setup_refuses_to_replace_a_different_registration(self):
        install = self.make_install()
        environment, calls = self.make_fake_codex(install, mismatched=True)

        result = self.run_setup(environment)

        self.assertEqual(result.returncode, 1)
        self.assertIn("different MCP registration", result.stderr)
        recorded = calls.read_text(encoding="utf-8").splitlines()
        self.assertFalse(any(line.startswith("mcp add youtube") for line in recorded))


if __name__ == "__main__":
    unittest.main()
