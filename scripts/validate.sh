#!/usr/bin/env bash

set -u -o pipefail

PASS_COUNT=0
WARN_COUNT=0
FAIL_COUNT=0

EXPECTED_SKILLS=(
  my-farm-advisor
  my-farm-breeding-trial-management
  my-farm-qtl-analysis
)

FORBIDDEN_PATHS=(
  superior-byte-works-wrighter
  superior-byte-works-google-timesfm-forecasting
  node_modules
  .cache
  data
)

FORBIDDEN_TRACKED_ASSETS=(
  countries.geojson
  states_usa.geojson
)

pass() {
  printf 'PASS: %s\n' "$1"
  PASS_COUNT=$((PASS_COUNT + 1))
}

warn() {
  printf 'WARN: %s\n' "$1"
  WARN_COUNT=$((WARN_COUNT + 1))
}

fail() {
  printf 'FAIL: %s\n' "$1"
  FAIL_COUNT=$((FAIL_COUNT + 1))
}

SCRIPT_DIR=$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(CDPATH='' cd -- "${SCRIPT_DIR}/.." && pwd)

cd "${REPO_ROOT}" || {
  printf 'FAIL: could not enter repo root %s\n' "${REPO_ROOT}"
  exit 1
}

HAS_GIT=0
TRACKED_FILES=()
if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  HAS_GIT=1
  mapfile -t TRACKED_FILES < <(git ls-files)
  pass 'Git worktree metadata available'
else
  warn 'Git metadata unavailable; tracked-file checks will be skipped'
fi

tracked_prefix() {
  local prefix=$1
  local path
  for path in "${TRACKED_FILES[@]}"; do
    if [[ "${path}" == "${prefix}" || "${path}" == "${prefix}"/* ]]; then
      return 0
    fi
  done
  return 1
}

tracked_basename() {
  local name=$1
  local path
  for path in "${TRACKED_FILES[@]}"; do
    case "${path}" in
      "${name}"|*/"${name}")
        return 0
        ;;
    esac
  done
  return 1
}

require_file() {
  local path=$1
  local label=$2
  if [[ -s "${path}" ]]; then
    pass "${label} present (${path})"
  elif [[ -e "${path}" ]]; then
    fail "${label} exists but is empty (${path})"
  else
    fail "${label} missing (${path})"
  fi
}

check_json_fields() {
  local json_path=$1
  shift

  python3 - "$json_path" "$@" <<'PY'
import json
import sys
from pathlib import Path

json_path = Path(sys.argv[1])
required = sys.argv[2:]
data = json.loads(json_path.read_text(encoding='utf-8'))
missing = [field for field in required if field not in data or data[field] in ('', None)]
if missing:
    print(', '.join(missing))
    raise SystemExit(1)
PY
}

check_skill_metadata() {
  local skill_dir=$1
  local skill_file="${skill_dir}/SKILL.md"
  local output

  if output=$(python3 - "$skill_file" <<'PY'
import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
text = path.read_text(encoding='utf-8')
match = re.search(r'(?ms)^---\s*$\n(.*?)\n^---\s*$', text)
if not match:
    print('missing YAML frontmatter')
    raise SystemExit(1)

frontmatter = match.group(1).splitlines()
top = {}
meta = {}
in_metadata = False

for raw_line in frontmatter:
    line = raw_line.rstrip()
    if not line.strip() or line.lstrip().startswith('#'):
        continue
    top_match = re.match(r'^([A-Za-z0-9_-]+):\s*(.*)$', line)
    meta_match = re.match(r'^\s+([A-Za-z0-9_-]+):\s*(.*)$', line)

    if top_match and not raw_line.startswith((' ', '\t')):
        key, value = top_match.groups()
        top[key] = value.strip()
        in_metadata = key == 'metadata'
        continue
    if in_metadata and meta_match:
        key, value = meta_match.groups()
        meta[key] = value.strip()
        continue
    if not raw_line.startswith((' ', '\t')):
        in_metadata = False

missing = []
if not top.get('name'):
    missing.append('name')
if 'description' not in top:
    missing.append('description')
if not (top.get('version') or meta.get('version') or meta.get('skill-version')):
    missing.append('version')
if not (top.get('author') or meta.get('author') or meta.get('skill-author')):
    missing.append('author')

if missing:
    print(', '.join(missing))
    raise SystemExit(1)
PY
  ); then
    pass "SKILL metadata fields present (${skill_dir})"
  else
    fail "SKILL metadata fields missing (${skill_dir}): ${output}"
  fi
}

check_skill_links() {
  local skill_dir=$1
  local skill_file="${skill_dir}/SKILL.md"
  local output

  if output=$(python3 - "$skill_file" <<'PY'
import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
text = path.read_text(encoding='utf-8')
targets = []
targets.extend(re.findall(r'!?\[[^\]]*\]\(([^)]+)\)', text))
targets.extend(re.findall(r'<((?:https?://)[^>]+)>', text))

invalid = []
for raw_target in targets:
    target = raw_target.strip()
    if not target:
        invalid.append('<empty>')
        continue
    if target.startswith('<') and target.endswith('>'):
        target = target[1:-1].strip()
    if target.startswith(('http://', 'https://', '#')):
        continue
    if re.match(r'^[A-Za-z][A-Za-z0-9+.-]*:', target) or target.startswith('//'):
        invalid.append(target)
        continue
    if target.startswith('/'):
        invalid.append(target)
        continue
    if any(ch.isspace() for ch in target):
        invalid.append(target)

if invalid:
    print('; '.join(sorted(set(invalid))))
    raise SystemExit(1)
PY
  ); then
    pass "SKILL links valid (${skill_dir})"
  else
    fail "Invalid SKILL link target (${skill_dir}): ${output}"
  fi
}

check_text_absent() {
  local path=$1
  local needle=$2
  local label=$3
  local output

  if output=$(python3 - "$path" "$needle" <<'PY'
import sys
from pathlib import Path

root = Path(sys.argv[1])
needle = sys.argv[2]
matches = []
for item in sorted(root.rglob('*')):
    if not item.is_file():
        continue
    try:
        text = item.read_text(encoding='utf-8')
    except UnicodeDecodeError:
        continue
    if needle in text:
        matches.append(str(item.relative_to(root)))

if matches:
    print(', '.join(matches))
    raise SystemExit(1)
PY
  ); then
    pass "${label} absent"
  else
    fail "${label} found in ${output}"
  fi
}

check_path_absent() {
  local path=$1

  if [[ -e "${path}" ]]; then
    fail "Forbidden path present (${path})"
  elif (( HAS_GIT == 1 )) && tracked_prefix "${path}"; then
    fail "Forbidden tracked path detected (${path}/)"
  else
    pass "Forbidden path absent (${path})"
  fi
}

check_asset_not_tracked() {
  local asset_name=$1
  local local_hit=''

  if (( HAS_GIT == 0 )); then
    warn "Skipped tracked asset check for ${asset_name} because git metadata is unavailable"
    return
  fi

  if tracked_basename "${asset_name}"; then
    fail "Forbidden asset is tracked (${asset_name})"
    return
  fi

  local_hit=$(python3 - "$asset_name" <<'PY'
import sys
from pathlib import Path

name = sys.argv[1]
for item in Path('.').rglob(name):
    if item.is_file():
        print(item)
        raise SystemExit(0)
raise SystemExit(1)
PY
  ) || true

  if [[ -n "${local_hit}" ]]; then
    warn "Forbidden asset exists locally but is untracked (${local_hit})"
  else
    pass "Forbidden asset not tracked (${asset_name})"
  fi
}

check_markdown_relative_links() {
  local output

  if (( HAS_GIT == 0 )); then
    warn 'Skipped markdown relative-link check because git metadata is unavailable'
    return
  fi

  if output=$(python3 - "${TRACKED_FILES[@]}" <<'PY'
import os
import re
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

root = Path('.').resolve()
tracked_files = sys.argv[1:]
markdown_files = [Path(path) for path in tracked_files if path.endswith(('.md', '.markdown'))]
link_pattern = re.compile(r'!?' + r'\[[^\]]*\]' + r'\(([^)]+)\)')
scheme_pattern = re.compile(r'^[A-Za-z][A-Za-z0-9+.-]*:')


def clean_target(raw_target):
    target = raw_target.strip()
    if not target:
        return ''
    if target.startswith('<'):
        end = target.find('>')
        if end != -1:
            target = target[1:end].strip()
    else:
        title_match = re.search(r'\s+["\'()]', target)
        if title_match:
            target = target[:title_match.start()].strip()
    return target


def should_skip(target):
    lowered = target.lower()
    return (
        not target
        or target.startswith('#')
        or target.startswith('//')
        or lowered.startswith(('http://', 'https://', 'mailto:'))
        or scheme_pattern.match(target) is not None
    )


def target_path(target):
    split_target = urlsplit(target)
    path = unquote(split_target.path)
    if not path:
        return None
    if path.startswith('/'):
        return root / path.lstrip('/')
    return Path(path)


def strip_fenced_code_blocks(text):
    return re.sub(r'(?ms)^```.*?^```', '', text)


missing = []
for markdown_file in markdown_files:
    source = root / markdown_file
    try:
        text = source.read_text(encoding='utf-8')
    except UnicodeDecodeError:
        continue
    text = strip_fenced_code_blocks(text)

    for raw_target in link_pattern.findall(text):
        target = clean_target(raw_target)
        if should_skip(target):
            continue
        parsed_path = target_path(target)
        if parsed_path is None:
            continue
        resolved = parsed_path if parsed_path.is_absolute() else source.parent / parsed_path
        resolved = resolved.resolve(strict=False)
        try:
            resolved.relative_to(root)
        except ValueError:
            missing.append(f'{markdown_file}: {target} -> outside repository')
            continue
        if not resolved.exists():
            missing.append(f'{markdown_file}: {target} -> {os.path.relpath(resolved, root)}')

if missing:
    print('\n'.join(missing))
    raise SystemExit(1)
PY
  ); then
    pass 'Markdown relative links valid'
  else
    if [[ -z "${output}" ]]; then
      fail 'Markdown relative link check failed without output'
      return
    fi
    while IFS= read -r line; do
      [[ -n "${line}" ]] && fail "Missing markdown relative link target (${line})"
    done <<<"${output}"
  fi
}

printf '== Validation profile: my-farm-advisor-skills ==\n'

require_file 'README.md' 'Root README'
require_file 'PROVENANCE.md' 'Root provenance file'
require_file 'IMPORT_MANIFEST.md' 'Root import manifest'

for skill_dir in "${EXPECTED_SKILLS[@]}"; do
  if [[ -d "${skill_dir}" ]]; then
    pass "Skill directory present (${skill_dir})"
  else
    fail "Skill directory missing (${skill_dir})"
    continue
  fi

  require_file "${skill_dir}/SKILL.md" "Skill manifest for ${skill_dir}"
  require_file "${skill_dir}/README.md" "Skill README for ${skill_dir}"
  require_file "${skill_dir}/PROVENANCE.md" "Skill provenance for ${skill_dir}"

  if [[ -s "${skill_dir}/SKILL.md" ]]; then
    check_skill_metadata "${skill_dir}"
    check_skill_links "${skill_dir}"
  fi
done

for forbidden_path in "${FORBIDDEN_PATHS[@]}"; do
  check_path_absent "${forbidden_path}"
done

for asset_name in "${FORBIDDEN_TRACKED_ASSETS[@]}"; do
  check_asset_not_tracked "${asset_name}"
done

check_markdown_relative_links

check_text_absent 'my-farm-qtl-analysis' 'scientific-skills/qtl-analysis/' 'Stale scientific-skills/qtl-analysis/ reference'

geoadmin_root=''
if [[ -d 'my-farm-advisor/data-pipeline/src/shared/geoadmin' ]]; then
  geoadmin_root='my-farm-advisor/data-pipeline/src/shared/geoadmin'
elif [[ -d 'my-farm-advisor/shared/geoadmin' ]]; then
  geoadmin_root='my-farm-advisor/shared/geoadmin'
elif [[ -d 'my-farm-advisor/data-pipeline/src/data/geoadmin' ]]; then
  geoadmin_root='my-farm-advisor/data-pipeline/src/data/geoadmin'
elif [[ -d 'shared/geoadmin' ]]; then
  geoadmin_root='shared/geoadmin'
elif [[ -d 'geoadmin' ]]; then
  geoadmin_root='geoadmin'
fi

if [[ -n "${geoadmin_root}" ]]; then
  pass "Geoadmin root detected (${geoadmin_root})"
  for level in l0_countries l1_states l2_counties; do
    metadata_path="${geoadmin_root}/${level}/metadata.json"
    require_file "${metadata_path}" "Geoadmin metadata ${level}"
    if [[ -s "${metadata_path}" ]]; then
      if check_json_fields "${metadata_path}" source_url archive_name output_geojson output_parquet >/dev/null; then
        pass "Geoadmin metadata fields present (${level})"
      else
        fail "Geoadmin metadata missing required fields (${level})"
      fi
    fi
  done
else
  warn 'Geoadmin metadata root missing'
fi

printf '\nSummary: %d pass, %d warn, %d fail\n' "${PASS_COUNT}" "${WARN_COUNT}" "${FAIL_COUNT}"

if (( FAIL_COUNT > 0 )); then
  printf 'RESULT: FAIL\n'
  exit 1
fi

printf 'RESULT: PASS\n'
exit 0
