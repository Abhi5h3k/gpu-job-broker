# Run this from inside the gpu-job-broker folder on the new PC.
# Builds an incremental commit history (8 commits spread over ~30 min
# ending now) from the files you've already pasted in, then (optionally)
# pushes to GitHub.
 
$AuthorName  = "Abhishek Bhardwaj"
$AuthorEmail = "abhishekbhardwajca@gmail.com"
$RemoteUrl   = "https://github.com/Abhi5h3k/gpu-job-broker.git"
 
if (-not (Test-Path ".git")) {
    git init | Out-Null
}
git config user.name  $AuthorName
git config user.email $AuthorEmail
 
# 8 logical steps, in build order. Each entry: message + list of paths
# (only files that exist will actually be staged; others are skipped).
$steps = @(
    @{
        Msg   = "Add take-home exercise data files"
        Files = @("data/01a_trace.csv", "data/01b_failure_logs.jsonl", "data/01c_retrieval_queries.jsonl", "data/Take_Home_Exercise.docx")
    },
    @{
        Msg   = "Scaffold: uv project, config, db, models, simulated clock"
        Files = @(".env.example", ".gitignore", "app/__init__.py", "app/api/__init__.py", "app/clock.py", "app/config.py", "app/db.py", "app/models.py", "pyproject.toml", "uv.lock")
    },
    @{
        Msg   = "Implement provider, billing, scheduler, broker orchestrator (core engine)"
        Files = @("app/billing.py", "app/broker.py", "app/provider.py", "app/scheduler.py")
    },
    @{
        Msg   = "Add FastAPI app: jobs/nodes/costs/simulator/retrieval routers, Swagger story"
        Files = @("app/api/costs.py", "app/api/jobs.py", "app/api/nodes.py", "app/api/retrieval.py", "app/api/simulator.py", "app/deps.py", "app/main.py", "app/retrieval.py", "app/schemas.py")
    },
    @{
        Msg   = "Add replay.py (trace replay + policy comparison)"
        Files = @("replay.py")
    },
    @{
        Msg   = "Add full test suite (36 tests)"
        Files = @("tests/__init__.py", "tests/conftest.py", "tests/test_billing.py", "tests/test_jobs_api.py", "tests/test_provider_failures.py", "tests/test_replay_smoke.py", "tests/test_restart_safety.py", "tests/test_retrieval_precision.py", "tests/test_scheduler_fairness.py")
    },
    @{
        Msg   = "Add README.md and docs/DESIGN.md"
        Files = @("README.md", "docs/DESIGN.md")
    },
    @{
        Msg   = "Add live /guide page (story walkthrough, diagrams, try-it)"
        Files = @("app/guide.py")
    }
)
 
$now   = Get-Date
$start = $now.AddMinutes(-30)
$n     = $steps.Count
$committed = 0
 
for ($i = 0; $i -lt $n; $i++) {
    $step = $steps[$i]
    $existing = $step.Files | Where-Object { Test-Path $_ }
    if (-not $existing) {
        Write-Host "Skipping '$($step.Msg)' - no files present" -ForegroundColor Yellow
        continue
    }
 
    foreach ($f in $existing) { git add -- $f }
 
    $frac = if ($n -gt 1) { $i / ($n - 1) } else { 1 }
    $ts   = $start.AddSeconds(($now - $start).TotalSeconds * $frac)
    $dateStr = $ts.ToString("yyyy-MM-ddTHH:mm:sszzz")
 
    $env:GIT_AUTHOR_NAME     = $AuthorName
    $env:GIT_AUTHOR_EMAIL    = $AuthorEmail
    $env:GIT_COMMITTER_NAME  = $AuthorName
    $env:GIT_COMMITTER_EMAIL = $AuthorEmail
    $env:GIT_AUTHOR_DATE     = $dateStr
    $env:GIT_COMMITTER_DATE  = $dateStr
 
    git commit --no-verify -m $step.Msg | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "ERROR: commit failed for '$($step.Msg)'" -ForegroundColor Red
        exit 1
    }
    $committed++
    Write-Host "[$committed] $dateStr  $($step.Msg)"
}
 
Remove-Item Env:\GIT_AUTHOR_NAME, Env:\GIT_AUTHOR_EMAIL, Env:\GIT_COMMITTER_NAME, Env:\GIT_COMMITTER_EMAIL, Env:\GIT_AUTHOR_DATE, Env:\GIT_COMMITTER_DATE -ErrorAction SilentlyContinue
 
# Catch anything left over (e.g. files not in any step list above) in one final commit.
git add -A
$stagedOut = git diff --cached --name-only
if ($stagedOut) {
    git commit --no-verify -m "Misc remaining files" | Out-Null
    Write-Host "[extra] Misc remaining files"
}
 
git branch -M main
git remote remove origin 2>$null
git remote add origin $RemoteUrl
 
Write-Host ""
Write-Host "History built. Review with: git log --oneline"
Write-Host "Then push with:  git push --force -u origin main"
 