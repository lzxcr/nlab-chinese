#!/usr/bin/env bash
# nLab-Chinese translation pipeline orchestration.
# Thin wrapper over scripts/cli.py plus Quartz build/deploy helpers.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="${PYTHON:-python3}"
CLI="$SCRIPT_DIR/scripts/cli.py"

# Load .env when present (both this script and the Python CLI consume it).
if [[ -f "$SCRIPT_DIR/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$SCRIPT_DIR/.env"
  set +a
fi

# nLab upstream (the source of truth for academic pages).
NLAB_REPO="${NLAB_REPO:-https://github.com/ncatlab/nlab-content.git}"
NLAB_DIR="${NLAB_DIR:-$SCRIPT_DIR/nlab-content}"

# Local GGUF model served by llama.cpp (see serve). The default is only a
# conventional repository-local path; choose and configure the actual model.
LLM_MODEL_PATH="${LLM_MODEL_PATH:-$SCRIPT_DIR/models/gemma-4-12b-it.gguf}"
LLM_PORT="${LLM_PORT:-8080}"
# Conservative local context default; adjust it for the selected model and
# available memory.
LLM_CTX="${LLM_CTX:-4096}"
# GPU layers to offload (99 = all). Set to 0 to force CPU-only.
LLM_NGL="${LLM_NGL:-99}"

# Logs written by src/nlab_pipeline/config.py.
LOG_DIR="${LOG_DIR:-$SCRIPT_DIR/logs}"

# Remote that receives the built site on the gh-pages branch.  It is deliberately
# opt-in: an implicit `origin` is too easy to aim at the wrong repository.
DEPLOY_REMOTE="${DEPLOY_REMOTE:-}"

c_blue=$'\033[1;34m'; c_green=$'\033[1;32m'; c_yellow=$'\033[1;33m'; c_red=$'\033[1;31m'; c_off=$'\033[0m'
info() { echo -e "${c_blue}[INFO]${c_off} $1"; }
ok()   { echo -e "${c_green}[SUCCESS]${c_off} $1"; }
warn() { echo -e "${c_yellow}[WARNING]${c_off} $1"; }
fail() { echo -e "${c_red}[ERROR]${c_off} $1"; }

latest_log() {
  ls -t "$LOG_DIR"/pipeline.log* 2>/dev/null | head -n 1
}

sync_repo() {
  info "正在检查并同步远端 nLab 核心学术仓库..."
  if [[ ! -d "$NLAB_DIR/.git" ]]; then
    info "首次克隆（shallow）: $NLAB_REPO"
    git clone --depth 1 "$NLAB_REPO" "$NLAB_DIR"
    ok "初始化克隆完成。"
  else
    local before after
    before="$(git -C "$NLAB_DIR" rev-parse HEAD)"
    # The clone tracks the upstream default branch; do not assume its name.
    git -C "$NLAB_DIR" pull --ff-only
    after="$(git -C "$NLAB_DIR" rev-parse HEAD)"
    if [[ "$before" != "$after" ]]; then
      ok "检测到远端有学术条目更新！更新摘要如下："
      git -C "$NLAB_DIR" log --oneline "${before}..${after}" | head -n 15
    else
      ok "本地源已经是最新状态。"
    fi
  fi
}

serve() {
  command -v llama-server >/dev/null 2>&1 || {
    fail "未找到 llama-server；请先安装 llama.cpp 并将其加入 PATH。"
    return 1
  }
  [[ -f "$LLM_MODEL_PATH" ]] || {
    fail "模型文件不存在: $LLM_MODEL_PATH（请在 .env 中设置 LLM_MODEL_PATH）。"
    return 1
  }
  info "启动 llama.cpp 推理服务（模型: $LLM_MODEL_PATH, GPU 层: $LLM_NGL）..."
  local extra=()
  # Preserve reasoning for DeepSeek-family files; direct-output models use a
  # zero reasoning budget to avoid returning an analysis without a translation.
  if [[ "${LLM_MODEL_PATH,,}" != *deepseek* ]]; then
    extra+=(--reasoning-budget 0)
  fi
  # Quantised KV cache keeps the conservative local preset memory-efficient.
  extra+=(--cache-type-k q8_0 --cache-type-v q8_0)
  exec llama-server -m "$LLM_MODEL_PATH" --host 127.0.0.1 --port "$LLM_PORT" \
    -c "$LLM_CTX" -ngl "$LLM_NGL" "${extra[@]}"
}

build() {
  info "调用 Quartz 生成静态资源..."
  (cd "$SCRIPT_DIR" && node quartz/bootstrap-cli.mjs build)
  ok "构建完毕。"
}

deploy() {
  local force_push="${1:-}"
  if [[ -n "$force_push" && "$force_push" != "--force" ]]; then
    fail "未知参数: $force_push（仅支持 --force）"
    return 1
  fi
  info "进入自动部署流程..."
  [[ -n "$DEPLOY_REMOTE" ]] || {
    fail "未配置 DEPLOY_REMOTE；请在 .env 中显式指定你有权推送的远程名。"
    return 1
  }
  local remote_url
  remote_url="$(git -C "$SCRIPT_DIR" remote get-url "$DEPLOY_REMOTE" 2>/dev/null)" || {
    fail "未找到远程仓库 [$DEPLOY_REMOTE]，请先添加（或设置 DEPLOY_REMOTE）。"
    return 1
  }
  build
  local public_dir="$SCRIPT_DIR/public"
  [[ -d "$public_dir" ]] || { fail "静态编译输出目录不存在，终止部署。"; return 1; }
  # Safety: pushing a freshly-built site overwrites the gh-pages branch on the
  # remote.  Force-push only when explicitly requested.
  if [[ -z "$force_push" ]]; then
    warn "即将推送 public/ 到 [$DEPLOY_REMOTE] 的 gh-pages 分支（$remote_url）。"
    read -r -p "确认继续? [y/N]: " answer
    [[ "$answer" =~ ^[Yy]$ ]] || { warn "已取消部署。"; return 1; }
  fi
  (
    cd "$public_dir"
    git init -q
    git config user.name "nLab-Chinese deploy"
    git config user.email "noreply@nlab-chinese.invalid"
    git add -A
    git commit -q -m "Automated Deploy via manage.sh: $(date '+%Y-%m-%d %H:%M:%S')" || true
    if [[ -n "$force_push" ]]; then
      local remote_head=""
      if git ls-remote --exit-code "$remote_url" refs/heads/gh-pages >/dev/null 2>&1; then
        git fetch -q "$remote_url" refs/heads/gh-pages
        remote_head="$(git rev-parse FETCH_HEAD)"
        git push --force-with-lease="refs/heads/gh-pages:$remote_head" \
          "$remote_url" HEAD:gh-pages
      else
        git push "$remote_url" HEAD:gh-pages
      fi
    else
      git push "$remote_url" HEAD:gh-pages
    fi
  )
  ok "🎉 自动部署成功！"
}

view_log() {
  local log; log="$(latest_log)"
  [[ -n "$log" ]] || { fail "未检测到任何日志生成，请先执行 translate。"; return 1; }
  info "实时追踪最新处理器日志流 [$log]（按 Ctrl+C 退出）:"
  tail -f "$log"
}

error_report() {
  local log; log="$(latest_log)"
  [[ -n "$log" ]] || { fail "日志文件尚不存在。"; return 1; }
  info "=== 过滤最新日志 [$log] 中的 [ERROR], [WARNING] ==="
  grep -E "\[ERROR\]|\[WARNING\]" "$log" || ok "十分完美！当前日志中未检测到任何降级错误。"
}

usage() {
  echo "可用命令:"
  echo "  $0 sync-repo     - 同步官方 nLab 核心学术 Git 仓库"
  echo "  $0 scan          - 磁盘增量扫描，同步中央状态库（SQLite）"
  echo "  $0 audit         - 交互式人工审查缺失/多义词条并补录词典"
  echo "  $0 tree          - 渲染虚拟类目结构树并导出待翻译项"
  echo "  $0 status        - 打印中央状态库统计摘要"
  echo "  $0 serve         - 前台启动 llama.cpp 推理服务（翻译前需要）"
  echo "  $0 translate     - 启动大模型增量汉化（别名: run-idle）"
  echo "  $0 build         - 编译为静态 Quartz 站点资产"
  echo "  $0 deploy [--force] - 推送编译资产至 gh-pages 分支（默认需确认）"
  echo "  $0 view-log      - 实时追踪最新时间戳日志流"
  echo "  $0 error-report  - 审计最新日志中的异常"
}

case "${1:-}" in
  sync-repo)     sync_repo ;;
  scan|scan-repo) $PYTHON "$CLI" scan "${@:2}" ;;
  audit|audit-dict) $PYTHON "$CLI" audit ;;
  tree|tree-report) $PYTHON "$CLI" tree ;;
  status)        $PYTHON "$CLI" status ;;
  translate|run-idle) $PYTHON "$CLI" translate "${@:2}" ;;
  serve)         serve ;;
  build)         build ;;
  deploy)        deploy "${2:-}" ;;
  view-log)      view_log ;;
  error-report)  error_report ;;
  ""|-h|--help)  usage ;;
  *)             usage; exit 2 ;;
esac
