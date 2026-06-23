import subprocess
import sys

PRD_PATH = "docs/product/prd.md"

CODE_PREFIXES = (
    "src/",
    "firmware/",
    "tests/",
)

CODE_FILES = {
    "pyproject.toml",
    "requirements.txt",
}


def git_staged_files() -> list[str]:
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
        check=True,
        capture_output=True,
        text=True,
    )
    return [path.replace("\\", "/") for path in result.stdout.splitlines() if path.strip()]


def is_code_change(path: str) -> bool:
    return path in CODE_FILES or path.startswith(CODE_PREFIXES)


def main() -> int:
    staged = git_staged_files()
    code_changes = [path for path in staged if is_code_change(path)]

    if not code_changes:
        return 0

    if PRD_PATH in staged:
        return 0

    print(
        "PRD 一致性检查失败：检测到代码更新，但本次提交未暂存 docs/product/prd.md。",
        file=sys.stderr,
    )
    print("", file=sys.stderr)
    print("需要同步 PRD 的代码变更：", file=sys.stderr)
    for path in code_changes:
        print(f"  - {path}", file=sys.stderr)
    print("", file=sys.stderr)
    print("请更新并暂存 docs/product/prd.md，然后重新提交。", file=sys.stderr)
    print("如果代码变更不影响产品需求，也请在 PRD 的变更记录中说明“无需需求变更”的原因。", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
