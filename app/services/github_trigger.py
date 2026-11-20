from __future__ import annotations

from typing import Tuple

import httpx


async def trigger_github_worker(
    *,
    github_token: str,
    github_repo: str,
    workflow_file: str = "worker.yml",
    ref: str = "main",
) -> Tuple[bool, str]:
    """Admin Botdan GitHub Actions worker workflow'ni qo'lda ishga tushiradi."""
    if not github_token or not github_repo:
        return (
            False,
            "GITHUB_TOKEN yoki GITHUB_REPO sozlanmagan. "
            "Vercel Environment Variables bo'limida kiriting.",
        )

    url = (
        f"https://api.github.com/repos/{github_repo.strip('/')}"
        f"/actions/workflows/{workflow_file}/dispatches"
    )
    headers = {
        "Authorization": f"Bearer {github_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(url, headers=headers, json={"ref": ref})

    if resp.status_code == 204:
        return True, "GitHub Actions Worker muvaffaqiyatli ishga tushirildi!"
    return False, f"GitHub API xatosi ({resp.status_code}): {resp.text[:250]}"
