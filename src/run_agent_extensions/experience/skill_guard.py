"""Structural and security checks on a skill directory, and an advisory linter.

Two layers, kept apart on purpose, both ported from hermes-agent
(``tools/skills_guard.py`` and ``tools/skill_linter.py``):

``scan_skill`` is the guard. Regex-based static analysis over every text file in a
skill directory (data exfiltration, prompt injection, destructive commands,
persistence, reverse shells, obfuscation, supply-chain fetches, privilege escalation,
embedded credentials), invisible Unicode, and structural anomalies (symlinks escaping
the directory, binaries, oversized files, too many files, stray executable bits). The
verdict is ``safe``, ``caution`` (a high-severity finding) or ``dangerous`` (a critical
one), and a trust-aware install policy decides what the verdict means for a given
source. For a skill the agent writes, a ``dangerous`` verdict rolls the write back and
``caution`` is reported; the guard runs only when ``EXPERIENCE_SKILL_GUARD`` is on.

``lint_content`` is the reviewer's checklist. It never blocks. It encodes the
authoring standards the review prompt asks for: a description that fits the index
budget without marketing words, a name matching its directory, the metadata block,
a "When to Use" section, no dangling support-file links, no scaffolding files,
``platforms:`` gating for POSIX-only scripts, and native tool names instead of shell
utilities in prose.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCANNER_VERSION = "skills-guard-v1"
SKILL_PROMPT_DESC_LIMIT = 60

# -- trust configuration ---------------------------------------------------------------

TRUSTED_REPOS = frozenset(
    {"openai/skills", "anthropics/skills", "huggingface/skills", "NVIDIA/skills"}
)

# Verdict → decision per trust level: (safe, caution, dangerous).
INSTALL_POLICY: dict[str, tuple[str, str, str]] = {
    "builtin": ("allow", "allow", "allow"),
    "trusted": ("allow", "allow", "block"),
    "community": ("allow", "block", "block"),
    # Agent-created: "ask" surfaces a dangerous verdict as an error to the agent, which
    # can retry without the flagged content.
    "agent-created": ("allow", "allow", "ask"),
}

VERDICT_INDEX = {"safe": 0, "caution": 1, "dangerous": 2}


@dataclass(frozen=True, slots=True)
class Finding:
    pattern_id: str
    severity: str  # critical | high | medium | low
    category: str
    file: str
    description: str
    line: int = 0
    match: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "pattern_id": self.pattern_id,
            "severity": self.severity,
            "category": self.category,
            "file": self.file,
            "line": self.line,
            "match": self.match,
            "description": self.description,
        }


@dataclass(slots=True)
class ScanResult:
    skill_name: str
    verdict: str  # safe | caution | dangerous
    findings: tuple[Finding, ...] = ()
    source: str = "agent-created"
    trust_level: str = "agent-created"
    scanned_at: str = ""
    summary: str = ""
    scan_provenance: dict[str, Any] = field(default_factory=dict)

    @property
    def blocked(self) -> bool:
        return self.verdict == "dangerous"

    def report(self) -> str:
        if not self.findings:
            return f"{self.skill_name}: clean scan"
        lines = [f"{self.skill_name}: {self.verdict} — {len(self.findings)} finding(s)"]
        lines.extend(
            f"  [{f.severity}] {f.file}{':' + str(f.line) if f.line else ''}: {f.description}"
            for f in self.findings[:12]
        )
        return "\n".join(lines)


# -- threat patterns: (regex, pattern_id, severity, category, description) -----------------

THREAT_PATTERNS: tuple[tuple[str, str, str, str, str], ...] = (
    # exfiltration: shell commands leaking secrets
    (
        r"curl\s+[^\n]*\$\{?\w*(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API)",
        "env_exfil_curl",
        "critical",
        "exfiltration",
        "curl command interpolating secret environment variable",
    ),
    (
        r"wget\s+[^\n]*\$\{?\w*(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API)",
        "env_exfil_wget",
        "critical",
        "exfiltration",
        "wget command interpolating secret environment variable",
    ),
    (
        r"fetch\s*\([^\n]*\$\{?\w*(KEY|TOKEN|SECRET|PASSWORD|API)",
        "env_exfil_fetch",
        "critical",
        "exfiltration",
        "fetch() call interpolating secret environment variable",
    ),
    (
        r"httpx?\.(get|post|put|patch)\s*\([^\n]*(KEY|TOKEN|SECRET|PASSWORD)",
        "env_exfil_httpx",
        "critical",
        "exfiltration",
        "HTTP library call with secret variable",
    ),
    (
        r"requests\.(get|post|put|patch)\s*\([^\n]*(KEY|TOKEN|SECRET|PASSWORD)",
        "env_exfil_requests",
        "critical",
        "exfiltration",
        "requests library call with secret variable",
    ),
    # exfiltration: reading credential stores
    (
        r"base64[^\n]*env",
        "encoded_exfil",
        "high",
        "exfiltration",
        "base64 encoding combined with environment access",
    ),
    (
        r"\$HOME/\.ssh|\~/\.ssh",
        "ssh_dir_access",
        "high",
        "exfiltration",
        "references user SSH directory",
    ),
    (
        r"\$HOME/\.aws|\~/\.aws",
        "aws_dir_access",
        "high",
        "exfiltration",
        "references user AWS credentials directory",
    ),
    (
        r"\$HOME/\.gnupg|\~/\.gnupg",
        "gpg_dir_access",
        "high",
        "exfiltration",
        "references user GPG keyring",
    ),
    (
        r"\$HOME/\.kube|\~/\.kube",
        "kube_dir_access",
        "high",
        "exfiltration",
        "references Kubernetes config directory",
    ),
    (
        r"\$HOME/\.docker|\~/\.docker",
        "docker_dir_access",
        "high",
        "exfiltration",
        "references Docker config (may contain registry creds)",
    ),
    (
        r"\$HOME/\.run/\.env|\~/\.run/\.env",
        "run_agent_env_access",
        "critical",
        "exfiltration",
        "directly references the Run Agent secrets file",
    ),
    # `cat <secrets-file>` reads credentials; `cat > file` writes one and is exempt.
    (
        r"cat\s+(?!>)[^\n]*(\.env|credentials|\.netrc|\.pgpass|\.npmrc|\.pypirc)",
        "read_secrets_file",
        "critical",
        "exfiltration",
        "reads known secrets file",
    ),
    # exfiltration: programmatic env access
    (
        r"printenv|env\s*\|",
        "dump_all_env",
        "high",
        "exfiltration",
        "dumps all environment variables",
    ),
    (
        r"os\.environ\b(?!\s*\.get\s*\(\s*[\"'](?![^\"']*(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)))",
        "python_os_environ",
        "high",
        "exfiltration",
        "accesses os.environ (potential env dump)",
    ),
    (
        r"os\.environ\s*\.get\s*\(\s*[\"'][^\"']*(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)",
        "python_environ_get_secret",
        "critical",
        "exfiltration",
        "reads secret via os.environ.get()",
    ),
    (
        r"os\.getenv\s*\(\s*[^\)]*(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)",
        "python_getenv_secret",
        "critical",
        "exfiltration",
        "reads secret via os.getenv()",
    ),
    (
        r"process\.env\[",
        "node_process_env",
        "high",
        "exfiltration",
        "accesses process.env (Node.js environment)",
    ),
    (
        r"ENV\[.*(?:KEY|TOKEN|SECRET|PASSWORD)",
        "ruby_env_secret",
        "critical",
        "exfiltration",
        "reads secret via Ruby ENV[]",
    ),
    # exfiltration: DNS and staging
    (
        r"(?<![-/])\b(dig|nslookup|host)\s+[^\n]*\$",
        "dns_exfil",
        "critical",
        "exfiltration",
        "DNS lookup with variable interpolation (possible DNS exfiltration)",
    ),
    (
        r">\s*/tmp/[^\s]*\s*&&\s*(curl|wget|nc|python)",
        "tmp_staging",
        "critical",
        "exfiltration",
        "writes to /tmp then exfiltrates",
    ),
    # exfiltration: markdown/link based
    (
        r"!\[.*\]\(https?://[^\)]*\$\{?",
        "md_image_exfil",
        "high",
        "exfiltration",
        "markdown image URL with variable interpolation (image-based exfil)",
    ),
    (
        r"\[.*\]\(https?://[^\)]*\$\{?",
        "md_link_exfil",
        "high",
        "exfiltration",
        "markdown link with variable interpolation",
    ),
    # prompt injection
    (
        r"ignore\s+(?:\w+\s+)*(previous|all|above|prior)\s+instructions",
        "prompt_injection_ignore",
        "critical",
        "injection",
        "prompt injection: ignore previous instructions",
    ),
    (
        r"忽略(?:之前|上面|所有|以上)的?(?:指令|指示|规则)",
        "prompt_injection_zh",
        "critical",
        "injection",
        "prompt injection (Chinese): ignore previous instructions",
    ),
    (
        r"you\s+are\s+(?:\w+\s+)*now\s+",
        "role_hijack",
        "high",
        "injection",
        "attempts to override the agent's role",
    ),
    (
        r"do\s+not\s+(?:\w+\s+)*tell\s+(?:\w+\s+)*the\s+user",
        "deception_hide",
        "critical",
        "injection",
        "instructs agent to hide information from user",
    ),
    (
        r"不要(?:告诉|让)用户(?:知道)?",
        "deception_hide_zh",
        "critical",
        "injection",
        "instructs agent to hide information from user (Chinese)",
    ),
    (
        r"system\s+(?:\w+\s+)*prompt\s+(?:\w+\s+)*override",
        "sys_prompt_override",
        "critical",
        "injection",
        "attempts to override the system prompt",
    ),
    (
        r"pretend\s+(?:\w+\s+)*(you\s+are|to\s+be)\s+",
        "role_pretend",
        "high",
        "injection",
        "attempts to make the agent assume a different identity",
    ),
    (
        r"disregard\s+(?:\w+\s+)*(your|all|any)\s+(?:\w+\s+)*(instructions|rules|guidelines)",
        "disregard_rules",
        "critical",
        "injection",
        "instructs agent to disregard its rules",
    ),
    (
        r"output\s+(?:\w+\s+)*(system|initial)\s+prompt",
        "leak_system_prompt",
        "high",
        "injection",
        "attempts to extract the system prompt",
    ),
    (
        r"(when|if)\s+no\s*one\s+is\s+(watching|looking)",
        "conditional_deception",
        "high",
        "injection",
        "conditional instruction to behave differently when unobserved",
    ),
    (
        r"act\s+as\s+(if|though)\s+(?:\w+\s+)*you\s+(?:\w+\s+)*(have\s+no|don't\s+have)\s+(?:\w+\s+)*(restrictions|limits|rules)",
        "bypass_restrictions",
        "critical",
        "injection",
        "instructs agent to act without restrictions",
    ),
    (
        r"translate\s+.*\s+into\s+.*\s+and\s+(execute|run|eval)",
        "translate_execute",
        "critical",
        "injection",
        "translate-then-execute evasion technique",
    ),
    (
        r"<!--[^>]*(?:ignore|override|system|secret|hidden)[^>]*-->",
        "html_comment_injection",
        "high",
        "injection",
        "hidden instructions in HTML comments",
    ),
    (
        r"<\s*div\s+style\s*=\s*[\"'][\s\S]*?display\s*:\s*none",
        "hidden_div",
        "high",
        "injection",
        "hidden HTML div (invisible instructions)",
    ),
    # destructive operations
    (
        r"rm\s+-rf\s+/",
        "destructive_root_rm",
        "critical",
        "destructive",
        "recursive delete from root",
    ),
    (
        r"rm\s+(-[^\s]*)?r.*\$HOME|\brmdir\s+.*\$HOME",
        "destructive_home_rm",
        "critical",
        "destructive",
        "recursive delete targeting home directory",
    ),
    (r"chmod\s+777", "insecure_perms", "medium", "destructive", "sets world-writable permissions"),
    (
        r">\s*/etc/",
        "system_overwrite",
        "critical",
        "destructive",
        "overwrites system configuration file",
    ),
    (r"\bmkfs\b", "format_filesystem", "critical", "destructive", "formats a filesystem"),
    (
        r"\bdd\s+.*if=.*of=/dev/",
        "disk_overwrite",
        "critical",
        "destructive",
        "raw disk write operation",
    ),
    (
        r"shutil\.rmtree\s*\(\s*[\"'/]",
        "python_rmtree",
        "high",
        "destructive",
        "Python rmtree on absolute or root-relative path",
    ),
    (
        r"truncate\s+-s\s*0\s+/",
        "truncate_system",
        "critical",
        "destructive",
        "truncates system file to zero bytes",
    ),
    # persistence
    (r"\bcrontab\b", "persistence_cron", "medium", "persistence", "modifies cron jobs"),
    (
        r"\.(bashrc|zshrc|profile|bash_profile|bash_login|zprofile|zlogin)\b",
        "shell_rc_mod",
        "medium",
        "persistence",
        "references shell startup file",
    ),
    (r"authorized_keys", "ssh_backdoor", "critical", "persistence", "modifies SSH authorized keys"),
    (r"ssh-keygen", "ssh_keygen", "medium", "persistence", "generates SSH keys"),
    (
        r"systemd.*\.service|systemctl\s+(enable|start)",
        "systemd_service",
        "medium",
        "persistence",
        "references or enables systemd service",
    ),
    (r"/etc/init\.d/", "init_script", "medium", "persistence", "references init.d startup script"),
    (
        r"launchctl\s+load|LaunchAgents|LaunchDaemons",
        "macos_launchd",
        "medium",
        "persistence",
        "macOS launch agent/daemon persistence",
    ),
    (
        r"/etc/sudoers|visudo",
        "sudoers_mod",
        "critical",
        "persistence",
        "modifies sudoers (privilege escalation)",
    ),
    (
        r"git\s+config\s+--global\s+",
        "git_config_global",
        "medium",
        "persistence",
        "modifies global git configuration",
    ),
    # network: reverse shells and tunnels
    (
        r"\bnc\s+-[lp]|ncat\s+-[lp]|\bsocat\b",
        "reverse_shell",
        "critical",
        "network",
        "potential reverse shell listener",
    ),
    (
        r"\bngrok\b|\blocaltunnel\b|\bserveo\b|\bcloudflared\b",
        "tunnel_service",
        "high",
        "network",
        "uses tunneling service for external access",
    ),
    (
        r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}:\d{2,5}",
        "hardcoded_ip_port",
        "medium",
        "network",
        "hardcoded IP address with port",
    ),
    (
        r"0\.0\.0\.0:\d+|INADDR_ANY",
        "bind_all_interfaces",
        "high",
        "network",
        "binds to all network interfaces",
    ),
    (
        r"/bin/(ba)?sh\s+-i\s+.*>/dev/tcp/",
        "bash_reverse_shell",
        "critical",
        "network",
        "bash interactive reverse shell via /dev/tcp",
    ),
    (
        r"python[23]?\s+-c\s+[\"']import\s+socket",
        "python_socket_oneliner",
        "critical",
        "network",
        "Python one-liner socket connection (likely reverse shell)",
    ),
    (
        r"socket\.connect\s*\(\s*\(",
        "python_socket_connect",
        "high",
        "network",
        "Python socket connect to arbitrary host",
    ),
    (
        r"webhook\.site|requestbin\.com|pipedream\.net|hookbin\.com",
        "exfil_service",
        "high",
        "network",
        "references known data exfiltration/webhook testing service",
    ),
    (
        r"pastebin\.com|hastebin\.com|ghostbin\.",
        "paste_service",
        "medium",
        "network",
        "references paste service (possible data staging)",
    ),
    # obfuscation: encoding and eval
    (
        r"base64\s+(-d|--decode)\s*\|",
        "base64_decode_pipe",
        "high",
        "obfuscation",
        "base64 decodes and pipes to execution",
    ),
    (
        r"\\x[0-9a-fA-F]{2}.*\\x[0-9a-fA-F]{2}.*\\x[0-9a-fA-F]{2}",
        "hex_encoded_string",
        "medium",
        "obfuscation",
        "hex-encoded string (possible obfuscation)",
    ),
    (r"\beval\s*\(\s*[\"']", "eval_string", "high", "obfuscation", "eval() with string argument"),
    (r"\bexec\s*\(\s*[\"']", "exec_string", "high", "obfuscation", "exec() with string argument"),
    (
        r"echo\s+[^\n]*\|\s*(bash|sh|python|perl|ruby|node)",
        "echo_pipe_exec",
        "critical",
        "obfuscation",
        "echo piped to interpreter for execution",
    ),
    (
        r"compile\s*\(\s*[^\)]+,\s*[\"'].*[\"']\s*,\s*[\"']exec[\"']\s*\)",
        "python_compile_exec",
        "high",
        "obfuscation",
        "Python compile() with exec mode",
    ),
    (
        r"getattr\s*\(\s*__builtins__",
        "python_getattr_builtins",
        "high",
        "obfuscation",
        "dynamic access to Python builtins (evasion technique)",
    ),
    (
        r"__import__\s*\(\s*[\"']os[\"']\s*\)",
        "python_import_os",
        "high",
        "obfuscation",
        "dynamic import of os module",
    ),
    (
        r"codecs\.decode\s*\(\s*[\"']",
        "python_codecs_decode",
        "medium",
        "obfuscation",
        "codecs.decode (possible ROT13 or encoding obfuscation)",
    ),
    (
        r"String\.fromCharCode|charCodeAt",
        "js_char_code",
        "medium",
        "obfuscation",
        "JavaScript character code construction (possible obfuscation)",
    ),
    (
        r"atob\s*\(|btoa\s*\(",
        "js_base64",
        "medium",
        "obfuscation",
        "JavaScript base64 encode/decode",
    ),
    (
        r"\[::-1\]",
        "string_reversal",
        "low",
        "obfuscation",
        "string reversal (possible obfuscated payload)",
    ),
    (
        r"chr\s*\(\s*\d+\s*\)\s*\+\s*chr\s*\(\s*\d+",
        "chr_building",
        "high",
        "obfuscation",
        "building string from chr() calls (obfuscation)",
    ),
    (
        r"\\u[0-9a-fA-F]{4}.*\\u[0-9a-fA-F]{4}.*\\u[0-9a-fA-F]{4}",
        "unicode_escape_chain",
        "medium",
        "obfuscation",
        "chain of unicode escapes (possible obfuscation)",
    ),
    # process execution in scripts
    (
        r"subprocess\.(run|call|Popen|check_output)\s*\(",
        "python_subprocess",
        "medium",
        "execution",
        "Python subprocess execution",
    ),
    (
        r"os\.system\s*\(",
        "python_os_system",
        "high",
        "execution",
        "os.system() — unguarded shell execution",
    ),
    (
        r"os\.popen\s*\(",
        "python_os_popen",
        "high",
        "execution",
        "os.popen() — shell pipe execution",
    ),
    (
        r"child_process\.(exec|spawn|fork)\s*\(",
        "node_child_process",
        "high",
        "execution",
        "Node.js child_process execution",
    ),
    (
        r"Runtime\.getRuntime\(\)\.exec\(",
        "java_runtime_exec",
        "high",
        "execution",
        "Java Runtime.exec() — shell execution",
    ),
    (
        r"`[^`]*\$\([^)]+\)[^`]*`",
        "backtick_subshell",
        "medium",
        "execution",
        "backtick string with command substitution",
    ),
    # path traversal
    (
        r"\.\./\.\./\.\.",
        "path_traversal_deep",
        "high",
        "traversal",
        "deep relative path traversal (3+ levels up)",
    ),
    (
        r"\.\./\.\.",
        "path_traversal",
        "medium",
        "traversal",
        "relative path traversal (2+ levels up)",
    ),
    (
        r"/etc/passwd|/etc/shadow",
        "system_passwd_access",
        "critical",
        "traversal",
        "references system password files",
    ),
    (
        r"/proc/self|/proc/\d+/",
        "proc_access",
        "high",
        "traversal",
        "references /proc filesystem (process introspection)",
    ),
    (
        r"/dev/shm/",
        "dev_shm",
        "medium",
        "traversal",
        "references shared memory (common staging area)",
    ),
    # crypto mining
    (
        r"xmrig|stratum\+tcp|monero|coinhive|cryptonight",
        "crypto_mining",
        "critical",
        "mining",
        "cryptocurrency mining reference",
    ),
    (
        r"hashrate|nonce.*difficulty",
        "mining_indicators",
        "medium",
        "mining",
        "possible cryptocurrency mining indicators",
    ),
    # supply chain: curl/wget pipe to shell
    (
        r"curl\s+[^\n]*\|\s*(ba)?sh",
        "curl_pipe_shell",
        "critical",
        "supply_chain",
        "curl piped to shell (download-and-execute)",
    ),
    (
        r"wget\s+[^\n]*-O\s*-\s*\|\s*(ba)?sh",
        "wget_pipe_shell",
        "critical",
        "supply_chain",
        "wget piped to shell (download-and-execute)",
    ),
    (
        r"curl\s+[^\n]*\|\s*python",
        "curl_pipe_python",
        "critical",
        "supply_chain",
        "curl piped to Python interpreter",
    ),
    # supply chain: unpinned/deferred dependencies
    (
        r"#\s*///\s*script.*dependencies",
        "pep723_inline_deps",
        "medium",
        "supply_chain",
        "PEP 723 inline script metadata with dependencies (verify pinning)",
    ),
    (
        r"pip\s+install\s+(?!-r\s)(?!.*==)",
        "unpinned_pip_install",
        "medium",
        "supply_chain",
        "pip install without version pinning",
    ),
    (
        r"npm\s+install\s+(?!.*@\d)",
        "unpinned_npm_install",
        "medium",
        "supply_chain",
        "npm install without version pinning",
    ),
    (
        r"uv\s+run\s+",
        "uv_run",
        "medium",
        "supply_chain",
        "uv run (may auto-install unpinned dependencies)",
    ),
    # supply chain: remote resource fetching
    (
        r"(curl|wget|httpx?\.get|requests\.get|fetch)\s*[\(]?\s*[\"']https?://",
        "remote_fetch",
        "medium",
        "supply_chain",
        "fetches remote resource at runtime",
    ),
    (
        r"git\s+clone\s+",
        "git_clone",
        "medium",
        "supply_chain",
        "clones a git repository at runtime",
    ),
    (
        r"docker\s+pull\s+",
        "docker_pull",
        "medium",
        "supply_chain",
        "pulls a Docker image at runtime",
    ),
    # privilege escalation
    (
        r"^allowed-tools\s*:",
        "allowed_tools_field",
        "low",
        "privilege_escalation",
        "skill declares allowed-tools (standard frontmatter; informational)",
    ),
    (r"\bsudo\b", "sudo_usage", "high", "privilege_escalation", "uses sudo (privilege escalation)"),
    (
        r"setuid|setgid|cap_setuid",
        "setuid_setgid",
        "critical",
        "privilege_escalation",
        "setuid/setgid (privilege escalation mechanism)",
    ),
    (
        r"NOPASSWD",
        "nopasswd_sudo",
        "critical",
        "privilege_escalation",
        "NOPASSWD sudoers entry (passwordless privilege escalation)",
    ),
    (
        r"chmod\s+[u+]?s",
        "suid_bit",
        "critical",
        "privilege_escalation",
        "sets SUID/SGID bit on a file",
    ),
    # agent config persistence
    (
        r"AGENTS\.md|CLAUDE\.md|\.cursorrules|\.clinerules",
        "agent_config_mod",
        "critical",
        "persistence",
        "references agent config files (could persist malicious instructions across sessions)",
    ),
    (
        r"\.run/settings\.json|\.run/USER\.md|\.run/MEMORY\.md",
        "run_agent_config_mod",
        "critical",
        "persistence",
        "references Run Agent configuration or memory files directly",
    ),
    (
        r"\.claude/settings|\.codex/config|\.hermes/config\.yaml",
        "other_agent_config",
        "high",
        "persistence",
        "references other agent configuration files",
    ),
    # hardcoded secrets
    (
        r"(?:api[_-]?key|token|secret|password)\s*[=:]\s*[\"'][A-Za-z0-9+/=_-]{20,}",
        "hardcoded_secret",
        "critical",
        "credential_exposure",
        "possible hardcoded API key, token, or secret",
    ),
    (
        r"-----BEGIN\s+(RSA\s+)?PRIVATE\s+KEY-----",
        "embedded_private_key",
        "critical",
        "credential_exposure",
        "embedded private key",
    ),
    (
        r"ghp_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{80,}",
        "github_token_leaked",
        "critical",
        "credential_exposure",
        "GitHub personal access token in skill content",
    ),
    (
        r"sk-[A-Za-z0-9]{20,}",
        "openai_key_leaked",
        "critical",
        "credential_exposure",
        "possible OpenAI API key in skill content",
    ),
    (
        r"sk-ant-[A-Za-z0-9_-]{90,}",
        "anthropic_key_leaked",
        "critical",
        "credential_exposure",
        "possible Anthropic API key in skill content",
    ),
    (
        r"AKIA[0-9A-Z]{16}",
        "aws_access_key_leaked",
        "critical",
        "credential_exposure",
        "AWS access key ID in skill content",
    ),
    (
        r"glpat-[A-Za-z0-9_\-]{20,}",
        "gitlab_token_leaked",
        "critical",
        "credential_exposure",
        "GitLab personal access token in skill content",
    ),
    # additional prompt injection: jailbreak patterns
    (
        r"\bDAN\s+mode\b|Do\s+Anything\s+Now",
        "jailbreak_dan",
        "critical",
        "injection",
        "DAN (Do Anything Now) jailbreak attempt",
    ),
    (
        r"\bdeveloper\s+mode\b.*\benabled?\b",
        "jailbreak_dev_mode",
        "critical",
        "injection",
        "developer mode jailbreak attempt",
    ),
    (
        r"hypothetical\s+scenario.*(?:ignore|bypass|override)",
        "hypothetical_bypass",
        "high",
        "injection",
        "hypothetical scenario used to bypass restrictions",
    ),
    (
        r"for\s+educational\s+purposes?\s+only",
        "educational_pretext",
        "medium",
        "injection",
        "educational pretext often used to justify harmful content",
    ),
    (
        r"(respond|answer|reply)\s+without\s+(?:\w+\s+)*(restrictions|limitations|filters|safety)",
        "remove_filters",
        "critical",
        "injection",
        "instructs agent to respond without safety filters",
    ),
    (
        r"you\s+have\s+been\s+(?:\w+\s+)*(updated|upgraded|patched)\s+to",
        "fake_update",
        "high",
        "injection",
        "fake update/patch announcement (social engineering)",
    ),
    (
        r"new\s+(?:\w+\s+)*policy|updated\s+(?:\w+\s+)*guidelines|revised\s+(?:\w+\s+)*instructions",
        "fake_policy",
        "medium",
        "injection",
        "claims new policy/guidelines (may be social engineering)",
    ),
    # context window exfiltration
    (
        r"(include|output|print|send|share)\s+(?:\w+\s+)*(conversation|chat\s+history|previous\s+messages|context)",
        "context_exfil",
        "high",
        "exfiltration",
        "instructs agent to output/share conversation history",
    ),
    (
        r"(send|post|upload|transmit)\s+.*\s+(to|at)\s+https?://",
        "send_to_url",
        "high",
        "exfiltration",
        "instructs agent to send data to a URL",
    ),
)

_COMPILED_THREAT_PATTERNS = tuple(
    (re.compile(pattern, re.IGNORECASE), pid, severity, category, description)
    for pattern, pid, severity, category, description in THREAT_PATTERNS
)

# -- structural limits ---------------------------------------------------------------------

MAX_FILE_COUNT = 50
MAX_TOTAL_SIZE_KB = 1024
MAX_SINGLE_FILE_KB = 256

SCANNABLE_EXTENSIONS = frozenset(
    {
        ".md",
        ".txt",
        ".py",
        ".sh",
        ".bash",
        ".ps1",
        ".js",
        ".ts",
        ".rb",
        ".yaml",
        ".yml",
        ".json",
        ".toml",
        ".cfg",
        ".ini",
        ".conf",
        ".html",
        ".css",
        ".xml",
        ".tex",
        ".r",
        ".jl",
        ".pl",
        ".php",
    }
)
SUSPICIOUS_BINARY_EXTENSIONS = frozenset(
    {
        ".exe",
        ".dll",
        ".so",
        ".dylib",
        ".bin",
        ".dat",
        ".com",
        ".msi",
        ".dmg",
        ".app",
        ".deb",
        ".rpm",
    }
)
_SCRIPT_EXTENSIONS = frozenset({".sh", ".bash", ".py", ".rb", ".pl"})

INVISIBLE_CHARS: dict[str, str] = {
    "​": "zero-width space",
    "‌": "zero-width non-joiner",
    "‍": "zero-width joiner",
    "⁠": "word joiner",
    "⁢": "invisible times",
    "⁣": "invisible separator",
    "⁤": "invisible plus",
    "﻿": "BOM/zero-width no-break space",
    "‪": "LTR embedding",
    "‫": "RTL embedding",
    "‬": "pop directional",
    "‭": "LTR override",
    "‮": "RTL override",
    "⁦": "LTR isolate",
    "⁧": "RTL isolate",
    "⁨": "first strong isolate",
    "⁩": "pop directional isolate",
}

_SKILL_IGNORE_FILENAMES = (".skillignore", ".clawhubignore")
_ALWAYS_IGNORED_NAMES = frozenset(_SKILL_IGNORE_FILENAMES)
_NEVER_IGNORABLE = frozenset({"SKILL.md"})

IgnoreMatcher = Callable[[str], bool]


# -- scanning ------------------------------------------------------------------------------


def scan_lines(text: str, rel: str) -> list[Finding]:
    """Match every threat pattern against each line, one finding per pattern per line."""
    findings: list[Finding] = []
    lines = text.split("\n")
    seen: set[tuple[str, int]] = set()
    for compiled, pid, severity, category, description in _COMPILED_THREAT_PATTERNS:
        for number, line in enumerate(lines, start=1):
            if (pid, number) in seen:
                continue
            if compiled.search(line):
                seen.add((pid, number))
                matched = line.strip()
                if len(matched) > 120:
                    matched = matched[:117] + "..."
                findings.append(Finding(pid, severity, category, rel, description, number, matched))
    for number, line in enumerate(lines, start=1):
        for char, name in INVISIBLE_CHARS.items():
            if char in line:
                findings.append(
                    Finding(
                        "invisible_unicode",
                        "high",
                        "injection",
                        rel,
                        f"invisible unicode character {name} (possible text hiding/injection)",
                        number,
                        f"U+{ord(char):04X} ({name})",
                    )
                )
                break
    return findings


def scan_file(path: Path, rel: str = "") -> list[Finding]:
    """Scan one file; only text files and SKILL.md are read."""
    rel = rel or path.name
    if path.suffix.lower() not in SCANNABLE_EXTENSIONS and path.name != "SKILL.md":
        return []
    try:
        content = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return []
    return scan_lines(content, rel)


def _load_skill_ignore(skill_dir: Path) -> IgnoreMatcher:
    """A matcher for the skill's ``.skillignore`` / ``.clawhubignore`` patterns.

    Gitignore-style basics: comments and blank lines skipped, a trailing ``/`` marks a
    directory, ``*``/``?`` globs, a leading ``/`` anchors to the skill root. The ignore
    files themselves are always excluded; ``SKILL.md`` can never be excluded.
    """
    patterns: list[str] = []
    for name in _SKILL_IGNORE_FILENAMES:
        candidate = skill_dir / name
        try:
            if candidate.is_file():
                for raw in candidate.read_text(encoding="utf-8").splitlines():
                    line = raw.strip()
                    if line and not line.startswith("#"):
                        patterns.append(line)
        except (UnicodeDecodeError, OSError):
            continue

    def ignore(rel: str) -> bool:
        rel_posix = Path(rel).as_posix()
        base = rel_posix.split("/")[-1]
        if base in _NEVER_IGNORABLE:
            return False
        if base in _ALWAYS_IGNORED_NAMES:
            return True
        for pattern in patterns:
            anchored = pattern.startswith("/")
            item = pattern.lstrip("/")
            is_dir = item.endswith("/")
            item = item.rstrip("/")
            if not item:
                continue
            if is_dir:
                if rel_posix == item or rel_posix.startswith(item + "/"):
                    return True
                if not anchored and ("/" + rel_posix + "/").find("/" + item + "/") != -1:
                    return True
                continue
            if fnmatch.fnmatch(rel_posix, item):
                return True
            if not anchored:
                if fnmatch.fnmatch(base, item):
                    return True
                if "/" not in item and any(
                    fnmatch.fnmatch(segment, item) for segment in rel_posix.split("/")
                ):
                    return True
                if rel_posix.startswith(item + "/"):
                    return True
        return False

    return ignore


def _check_structure(skill_dir: Path, ignore: IgnoreMatcher | None = None) -> list[Finding]:
    """Too many files, too much data, binaries, escaping symlinks, stray executable bits."""
    matcher: IgnoreMatcher = ignore or (lambda _rel: False)
    findings: list[Finding] = []
    count = total = 0
    for path in skill_dir.rglob("*"):
        if not path.is_file() and not path.is_symlink():
            continue
        rel = str(path.relative_to(skill_dir))
        if matcher(rel):
            continue
        count += 1
        if path.is_symlink():
            try:
                resolved = path.resolve()
                if not resolved.is_relative_to(skill_dir.resolve()):
                    findings.append(
                        Finding(
                            "symlink_escape",
                            "critical",
                            "traversal",
                            rel,
                            "symlink points outside the skill directory",
                            0,
                            f"symlink -> {resolved}",
                        )
                    )
            except OSError:
                findings.append(
                    Finding(
                        "broken_symlink",
                        "medium",
                        "traversal",
                        rel,
                        "broken or circular symlink",
                        0,
                        "broken symlink",
                    )
                )
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        size = stat.st_size
        total += size
        if size > MAX_SINGLE_FILE_KB * 1024:
            findings.append(
                Finding(
                    "oversized_file",
                    "medium",
                    "structural",
                    rel,
                    f"file is {size // 1024}KB (limit: {MAX_SINGLE_FILE_KB}KB)",
                    0,
                    f"{size // 1024}KB",
                )
            )
        ext = path.suffix.lower()
        if ext in SUSPICIOUS_BINARY_EXTENSIONS:
            findings.append(
                Finding(
                    "binary_file",
                    "critical",
                    "structural",
                    rel,
                    f"binary/executable file ({ext}) should not be in a skill",
                    0,
                    f"binary: {ext}",
                )
            )
        # Windows derives the executable bit from the extension, so the check is
        # meaningful only where the bit is a real permission.
        if sys.platform != "win32" and ext not in _SCRIPT_EXTENSIONS and stat.st_mode & 0o111:
            findings.append(
                Finding(
                    "unexpected_executable",
                    "medium",
                    "structural",
                    rel,
                    "file has executable permission but is not a recognized script type",
                    0,
                    "executable bit set",
                )
            )
    if count > MAX_FILE_COUNT:
        findings.append(
            Finding(
                "too_many_files",
                "medium",
                "structural",
                "(directory)",
                f"skill has {count} files (limit: {MAX_FILE_COUNT})",
                0,
                f"{count} files",
            )
        )
    if total > MAX_TOTAL_SIZE_KB * 1024:
        findings.append(
            Finding(
                "oversized_skill",
                "high",
                "structural",
                "(directory)",
                f"skill is {total // 1024}KB total (limit: {MAX_TOTAL_SIZE_KB}KB)",
                0,
                f"{total // 1024}KB total",
            )
        )
    return findings


def _resolve_trust_level(source: str) -> str:
    normalized = source
    for prefix in ("skills-sh/", "skills.sh/", "skils-sh/", "skils.sh/"):
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix) :]
            break
    if normalized == "agent-created":
        return "agent-created"
    if normalized == "official":
        return "builtin"
    for trusted in TRUSTED_REPOS:
        if normalized == trusted or normalized.startswith(f"{trusted}/"):
            return "trusted"
    return "community"


def _determine_verdict(findings: list[Finding]) -> str:
    if not findings:
        return "safe"
    severities = {finding.severity for finding in findings}
    if "critical" in severities:
        return "dangerous"
    if "high" in severities:
        return "caution"
    return "safe"


def _build_summary(name: str, verdict: str, findings: list[Finding]) -> str:
    if not findings:
        return f"{name}: clean scan, no threats detected"
    categories = sorted({finding.category for finding in findings})
    return f"{name}: {verdict} — {len(findings)} finding(s) in {', '.join(categories)}"


def scan_skill(skill_dir: Path, source: str = "agent-created") -> ScanResult:
    """Scan a whole skill directory; the verdict decides whether a write may stand.

    Structural checks first, then every text file against the threat patterns, then
    invisible Unicode. A ``.skillignore`` excludes development artifacts from both;
    ``SKILL.md`` is always scanned.
    """
    trust = _resolve_trust_level(source)
    findings: list[Finding] = []
    if skill_dir.is_dir():
        ignore = _load_skill_ignore(skill_dir)
        findings.extend(_check_structure(skill_dir, ignore))
        for path in sorted(p for p in skill_dir.rglob("*") if p.is_file()):
            rel = str(path.relative_to(skill_dir))
            if ignore(rel):
                continue
            findings.extend(scan_file(path, rel))
    elif skill_dir.is_file():
        findings.extend(scan_file(skill_dir, skill_dir.name))
    verdict = _determine_verdict(findings)
    return ScanResult(
        skill_name=skill_dir.name,
        verdict=verdict,
        findings=tuple(findings),
        source=source,
        trust_level=trust,
        scanned_at=datetime.now(UTC).isoformat(),
        summary=_build_summary(skill_dir.name, verdict, findings),
    )


def scan_text(text: str, label: str) -> ScanResult:
    """Scan one piece of content before it touches disk."""
    findings = scan_lines(text, label)
    verdict = _determine_verdict(findings)
    return ScanResult(
        skill_name=label,
        verdict=verdict,
        findings=tuple(findings),
        scanned_at=datetime.now(UTC).isoformat(),
        summary=_build_summary(label, verdict, findings),
    )


def should_allow_install(result: ScanResult, force: bool = False) -> tuple[bool | None, str]:
    """Whether a scanned skill may be installed: (allowed, reason).

    ``None`` means the caller must ask for confirmation. A dangerous verdict from a
    community or trusted source cannot be forced.
    """
    policy = INSTALL_POLICY.get(result.trust_level, INSTALL_POLICY["community"])
    decision = policy[VERDICT_INDEX.get(result.verdict, 2)]
    if decision == "allow":
        return True, f"Allowed ({result.trust_level} source, {result.verdict} verdict)"
    if force and not (
        result.verdict == "dangerous" and result.trust_level in {"community", "trusted"}
    ):
        return True, (
            f"Force-installed despite {result.verdict} verdict ({len(result.findings)} findings)"
        )
    if decision == "ask":
        return None, (
            f"Requires confirmation ({result.trust_level} source + {result.verdict} verdict, "
            f"{len(result.findings)} findings)"
        )
    if result.verdict == "dangerous" and result.trust_level in {"community", "trusted"}:
        return False, (
            f"Blocked ({result.trust_level} source + dangerous verdict, "
            f"{len(result.findings)} findings). --force does not override a dangerous verdict."
        )
    return False, (
        f"Blocked ({result.trust_level} source + {result.verdict} verdict, "
        f"{len(result.findings)} findings). Use --force to override."
    )


def format_scan_report(result: ScanResult) -> str:
    """A compact multi-line report for the terminal or a chat."""
    lines = [
        f"Scan: {result.skill_name} ({result.source}/{result.trust_level})  "
        f"Verdict: {result.verdict.upper()}"
    ]
    if result.findings:
        order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        for finding in sorted(result.findings, key=lambda f: order.get(f.severity, 4)):
            sev = finding.severity.upper().ljust(8)
            cat = finding.category.ljust(14)
            loc = f"{finding.file}:{finding.line}".ljust(30)
            lines.append(f'  {sev} {cat} {loc} "{finding.match[:60]}"')
        lines.append("")
    allowed, reason = should_allow_install(result)
    status = (
        "ALLOWED" if allowed is True else ("NEEDS CONFIRMATION" if allowed is None else "BLOCKED")
    )
    lines.append(f"Decision: {status} — {reason}")
    return "\n".join(lines)


def _content_digest(skill_path: Path) -> str:
    """SHA-256 over relative POSIX paths and exact bytes, OS-independent."""
    digest = hashlib.sha256()
    if skill_path.is_dir():
        entries = sorted(
            (path.relative_to(skill_path).as_posix(), path)
            for path in skill_path.rglob("*")
            if path.is_file()
        )
        for rel, path in entries:
            digest.update(rel.encode("utf-8") + b"\x00")
            digest.update(path.read_bytes())
    else:
        digest.update(skill_path.read_bytes())
    return digest.hexdigest()


def full_content_hash(skill_path: Path) -> str:
    return f"sha256:{_content_digest(skill_path)}"


def content_hash(skill_path: Path) -> str:
    """A short integrity hash of every file in a skill directory."""
    return f"sha256:{_content_digest(skill_path)[:16]}"


def scan_skill_cached(
    skill_path: Path,
    source: str = "agent-created",
    *,
    source_url: str = "",
    cache_dir: Path | None = None,
) -> tuple[ScanResult, dict[str, Any]]:
    """Scan with an attestation cached against the exact current content."""
    bundle_hash = full_content_hash(skill_path)
    cache_root = cache_dir or skill_path.parent / ".scan-cache"
    source_identity = hashlib.sha256(f"{source}\0{source_url}".encode()).hexdigest()[:16]
    cache_file = cache_root / f"{bundle_hash.split(':', 1)[1]}-{source_identity}.json"
    cached: Any = None
    try:
        cached = json.loads(cache_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        cached = None
    if (
        isinstance(cached, dict)
        and cached.get("bundle_hash") == bundle_hash
        and cached.get("scanner_version") == SCANNER_VERSION
        and cached.get("source") == source
        and cached.get("source_url") == source_url
    ):
        provenance = dict(cached)
        provenance["fresh"] = False
        result = ScanResult(
            skill_name=skill_path.name,
            verdict=str(cached["verdict"]),
            findings=tuple(Finding(**item) for item in cached.get("findings", [])),
            source=source,
            trust_level=str(cached["trust_level"]),
            scanned_at=str(cached["scanned_at"]),
            summary=str(cached.get("summary", "")),
            scan_provenance=provenance,
        )
        return result, provenance
    result = scan_skill(skill_path, source=source)
    findings = [finding.as_dict() for finding in result.findings]
    provenance = {
        "source": source,
        "source_url": source_url,
        "bundle_hash": bundle_hash,
        "scanner_version": SCANNER_VERSION,
        "verdict": result.verdict,
        "trust_level": result.trust_level,
        "findings": findings,
        "rules": sorted({item["pattern_id"] for item in findings}),
        "scanned_at": result.scanned_at,
        "summary": result.summary,
        "fresh": True,
    }
    try:
        cache_root.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(provenance, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass
    result.scan_provenance = provenance
    return result, provenance


# -- advisory linter ----------------------------------------------------------------

ERROR = "error"
WARNING = "warning"

# Shell utilities the agent already has as native tools. Naming them in prose steers the
# model to a raw shell call instead of the tool that carries the guard rails.
_SHELL_UTIL_TO_TOOL: dict[str, str] = {
    "cat": "read",
    "head": "read",
    "tail": "read",
    "sed": "edit",
    "awk": "edit",
    "rg": "grep",
    "grep": "grep",
    "ls": "find",
}
_MARKETING_WORDS = (
    "powerful",
    "comprehensive",
    "seamless",
    "advanced",
    "cutting-edge",
    "state-of-the-art",
    "revolutionary",
    "robust",
)
_POSIX_PRIMITIVES = (
    "fcntl",
    "termios",
    "os.setsid",
    "signal.SIGKILL",
    "osascript",
    "/proc/",
    "apt-get",
    "systemctl",
)
_FORBIDDEN_FILES = ("README.md", "CHANGELOG.md", "install.sh", ".env", ".env.example", ".gitignore")
_EXPECTED_SECTIONS = ("When to Use", "When to use")
_VALID_PLATFORMS = frozenset({"linux", "macos", "windows", "darwin"})
_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]*")


@dataclass(frozen=True, slots=True)
class LintFinding:
    severity: str  # error | warning
    rule: str
    message: str

    def format(self) -> str:
        return f"{'✗' if self.severity == ERROR else '⚠'} [{self.rule}] {self.message}"


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Parse the YAML-shaped frontmatter a skill carries, nested blocks included.

    Handles ``key: value``, ``key:`` followed by an indented block, and ``[a, b]`` inline
    lists, which is all the skill frontmatter ever uses. Malformed input yields a flat
    best-effort mapping rather than an exception.
    """
    normalized = text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.startswith("---\n"):
        return {}, normalized
    end = normalized.find("\n---", 4)
    if end == -1:
        return {}, normalized
    raw = normalized[4:end]
    body = normalized[end + len("\n---") :]
    if body.startswith("\n"):
        body = body[1:]
    root: dict[str, Any] = {}
    stack: list[tuple[int, dict[str, Any]]] = [(-1, root)]
    for line in raw.splitlines():
        if not line.strip() or line.strip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        stripped = line.strip()
        while stack and indent <= stack[-1][0]:
            stack.pop()
        if not stack:
            stack.append((-1, root))
        container = stack[-1][1]
        key, separator, value = stripped.partition(":")
        if not separator:
            continue
        key = key.strip()
        value = value.strip()
        if value == "":
            child: dict[str, Any] = {}
            container[key] = child
            stack.append((indent, child))
        else:
            container[key] = _scalar(value)
    return root, body


def _scalar(value: str) -> Any:
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        return (
            [item.strip().strip("\"'") for item in inner.split(",") if item.strip()]
            if inner
            else []
        )
    lowered = value.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    return value.strip("\"'")


def _strip_code_blocks(body: str) -> str:
    return re.sub(r"```.*?```", "", body, flags=re.S)


def _check_name(frontmatter: dict[str, Any], skill_dir: Path | None) -> list[LintFinding]:
    findings: list[LintFinding] = []
    name = str(frontmatter.get("name", "")).strip()
    if not name:
        return findings
    if not _NAME_RE.fullmatch(name):
        findings.append(
            LintFinding(
                ERROR,
                "name-format",
                f"name '{name}' must be lowercase letters, digits, hyphens, and underscores only.",
            )
        )
    if skill_dir is not None and name != skill_dir.name:
        findings.append(
            LintFinding(
                ERROR,
                "name-dir-mismatch",
                f"frontmatter name '{name}' does not match directory '{skill_dir.name}'; "
                "they must be identical.",
            )
        )
    return findings


def _check_description(frontmatter: dict[str, Any]) -> list[LintFinding]:
    findings: list[LintFinding] = []
    description = str(frontmatter.get("description", "")).strip().strip("'\"")
    if not description:
        return findings
    if len(description) > SKILL_PROMPT_DESC_LIMIT:
        findings.append(
            LintFinding(
                WARNING,
                "description-length",
                f"description is {len(description)} chars; the skill index truncates past "
                f"{SKILL_PROMPT_DESC_LIMIT} chars + '...', losing routing signal. Keep it to one "
                "sentence.",
            )
        )
    lowered = description.lower()
    hits = [w for w in _MARKETING_WORDS if re.search(rf"\b{re.escape(w)}\b", lowered)]
    if hits:
        findings.append(
            LintFinding(
                WARNING,
                "description-marketing",
                f"description contains marketing words {hits}; state the capability, not "
                "adjectives.",
            )
        )
    name = str(frontmatter.get("name", "")).strip()
    if name and name.replace("-", " ").replace("_", " ") in lowered:
        findings.append(
            LintFinding(
                WARNING,
                "description-repeats-name",
                "description repeats the skill name; spend the characters on the trigger.",
            )
        )
    return findings


def _check_metadata_block(frontmatter: dict[str, Any]) -> list[LintFinding]:
    findings: list[LintFinding] = []
    for key in ("version", "author", "license"):
        if key not in frontmatter:
            findings.append(
                LintFinding(
                    WARNING,
                    "missing-metadata",
                    f"frontmatter is missing '{key}'; every peer skill has it.",
                )
            )
    meta = frontmatter.get("metadata")
    nested = meta.get("run_agent") if isinstance(meta, dict) else None
    if not isinstance(nested, dict):
        findings.append(
            LintFinding(
                WARNING,
                "missing-metadata",
                "frontmatter is missing metadata.run_agent.{tags, related_skills}.",
            )
        )
    elif "tags" not in nested:
        findings.append(
            LintFinding(WARNING, "missing-metadata", "metadata.run_agent.tags is missing.")
        )
    author = str(frontmatter.get("author", ""))
    if (
        author
        and author.strip().lower() in {"run agent", "agent", "run-agent"}
        and author != "Run Agent"
    ):
        findings.append(
            LintFinding(
                WARNING,
                "author-caps",
                f"author '{author}' should be 'Run Agent' (proper caps) or a real "
                "contributor name.",
            )
        )
    return findings


def _check_shell_utilities(body: str) -> list[LintFinding]:
    findings: list[LintFinding] = []
    prose = _strip_code_blocks(body)
    for util, tool in _SHELL_UTIL_TO_TOOL.items():
        if re.search(rf"`{re.escape(util)}`", prose):
            findings.append(
                LintFinding(
                    WARNING,
                    "shell-utility-reference",
                    f"prose references `{util}`; name the native tool `{tool}` instead.",
                )
            )
    return findings


def _check_sections(body: str) -> list[LintFinding]:
    if any(re.search(rf"^#+\s+{re.escape(s)}", body, re.M) for s in _EXPECTED_SECTIONS):
        return []
    return [
        LintFinding(
            WARNING,
            "missing-section",
            "no '## When to Use' section found; skills need explicit trigger conditions near "
            "the top.",
        )
    ]


def _check_reference_links(body: str, skill_dir: Path | None) -> list[LintFinding]:
    if skill_dir is None:
        return []
    findings: list[LintFinding] = []
    seen: set[str] = set()
    # scripts/ is excluded: dev skills mention repo-root scripts that live elsewhere.
    for match in re.finditer(r"(references|templates|assets)/[\w./-]+", body):
        rel = match.group(0)
        if rel in seen or "*" in rel or rel.endswith("/"):
            continue
        seen.add(rel)
        if not (skill_dir / rel).exists():
            findings.append(
                LintFinding(
                    WARNING,
                    "dangling-reference",
                    f"body references '{rel}' but that file does not exist in the skill directory.",
                )
            )
    return findings


def _check_platforms_gating(
    frontmatter: dict[str, Any], skill_dir: Path | None
) -> list[LintFinding]:
    if skill_dir is None or frontmatter.get("platforms"):
        return []
    scripts_dir = skill_dir / "scripts"
    if not scripts_dir.is_dir():
        return []
    offenders: dict[str, list[str]] = {}
    for script in scripts_dir.rglob("*"):
        if not script.is_file() or script.suffix not in {".py", ".sh", ".bash"}:
            continue
        try:
            text = script.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        hits = [p for p in _POSIX_PRIMITIVES if p in text]
        if hits:
            offenders[script.name] = hits
    if not offenders:
        return []
    detail = "; ".join(f"{k}: {v}" for k, v in offenders.items())
    return [
        LintFinding(
            WARNING,
            "platforms-gating",
            f"scripts use POSIX-only primitives ({detail}) but no 'platforms:' frontmatter is "
            "declared. Fix cross-platform or gate with platforms: [linux, macos].",
        )
    ]


def _check_platform_list_valid(frontmatter: dict[str, Any]) -> list[LintFinding]:
    platforms = frontmatter.get("platforms")
    if not platforms:
        return []
    items = platforms if isinstance(platforms, list) else [platforms]
    bad = [p for p in items if str(p).lower() not in _VALID_PLATFORMS]
    if not bad:
        return []
    return [
        LintFinding(
            WARNING,
            "platforms-value",
            f"platforms contains unrecognized value(s) {bad}; expected a subset of "
            f"{sorted(_VALID_PLATFORMS)}.",
        )
    ]


def _check_forbidden_files(skill_dir: Path | None) -> list[LintFinding]:
    if skill_dir is None:
        return []
    return [
        LintFinding(
            WARNING,
            "forbidden-file",
            f"skill ships '{name}'; skills should not include scaffolding/config files.",
        )
        for name in _FORBIDDEN_FILES
        if (skill_dir / name).exists()
    ]


def lint_content(text: str, *, skill_dir: Path | None = None) -> list[LintFinding]:
    """Lint raw SKILL.md content; pass ``skill_dir`` to enable the on-disk checks."""
    frontmatter, body = parse_frontmatter(text)
    findings: list[LintFinding] = []
    findings += _check_name(frontmatter, skill_dir)
    findings += _check_description(frontmatter)
    findings += _check_metadata_block(frontmatter)
    findings += _check_platform_list_valid(frontmatter)
    findings += _check_shell_utilities(body)
    findings += _check_sections(body)
    findings += _check_reference_links(body, skill_dir)
    findings += _check_platforms_gating(frontmatter, skill_dir)
    findings += _check_forbidden_files(skill_dir)
    return findings


def lint_skill(skill_md: Path) -> list[LintFinding]:
    try:
        text = skill_md.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []
    return lint_content(text, skill_dir=skill_md.parent)


def format_findings(findings: list[LintFinding]) -> str:
    return "\n".join(finding.format() for finding in findings)


def has_errors(findings: list[LintFinding]) -> bool:
    return any(finding.severity == ERROR for finding in findings)


__all__ = [
    "INSTALL_POLICY",
    "MAX_FILE_COUNT",
    "MAX_SINGLE_FILE_KB",
    "MAX_TOTAL_SIZE_KB",
    "SCANNER_VERSION",
    "SKILL_PROMPT_DESC_LIMIT",
    "THREAT_PATTERNS",
    "TRUSTED_REPOS",
    "Finding",
    "LintFinding",
    "ScanResult",
    "content_hash",
    "format_findings",
    "format_scan_report",
    "full_content_hash",
    "has_errors",
    "lint_content",
    "lint_skill",
    "parse_frontmatter",
    "scan_file",
    "scan_lines",
    "scan_skill",
    "scan_skill_cached",
    "scan_text",
    "should_allow_install",
]
