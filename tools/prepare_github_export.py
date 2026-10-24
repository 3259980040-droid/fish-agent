"""Prepare source/UI/spec tickets without exporting runtime data or Git history."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
BASE = Path(r'F:\AIWork\DSH\项目\DSH-Fish')
DEST = BASE / '交付/2026-10-24/GitHub发布包'
SECRET = re.compile(r'(?:gh[pousr]_[A-Za-z0-9]{24,}|github_pat_[A-Za-z0-9_]{30,}|sk-[A-Za-z0-9_-]{24,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)')


def main():
    copied, flagged = [], []
    def copy(source, target):
        try:
            if SECRET.search(source.read_text(encoding='utf-8-sig')):
                flagged.append(str(source)); return
        except UnicodeDecodeError:
            pass
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        copied.append(target.relative_to(DEST).as_posix())
    roots = {'AGENTS.md','GLOSSARY.md','README.md','ARCHITECTURE.md','CHANNEL-SPEC.md','PRODUCT.md','THIRD-PARTY.md','FISH-QUICKSTART.md','requirements.txt','requirements-memory.txt','requirements-build.txt','launch_dsh_fish.py','launch_fish.py','.gitignore','Fish.spec','FishDebug.spec'}
    sections = {'channels','fish_desktop','harness','scripts','packaging','dashboard','docs'}
    tool_files = {'migrate_agent_environment.py','migrate_attachment_resources.py','migrate_fish_schedules.py','migrate_qq_tool_grant.py','prepare_context_tokenizer.py','verify_unified_agent.py','prepare_github_export.py'}
    suffixes = {'.py','.md','.json','.css','.js','.ts','.tsx','.html','.svg','.ico','.png','.txt','.ps1','.cmd','.toml','.spec'}
    ignored = {'runtime','secrets','fixtures','maintenance','__pycache__','node_modules'}
    tracked = subprocess.check_output(['git','ls-files','-c','-o','--exclude-standard','-z'],cwd=ROOT).decode('utf-8').split('\0')
    for name in sorted(set(filter(None, tracked))):
        path, relative = ROOT/name, Path(name)
        if not path.is_file(): continue
        parts = relative.parts
        keep = name in roots or (parts[0] in sections and path.suffix.lower() in suffixes) or (parts[0]=='tools' and path.name in tool_files)
        if any(part in ignored for part in parts): keep = False
        if path.suffix=='.json' and 'persona-presets' not in parts and not path.name.endswith('.example.json'): keep = False
        if parts[0]=='docs' and len(parts)>1 and parts[1]!='agents': keep = False
        if keep: copy(path,DEST/'agent'/relative)
    ui = ROOT.parent/'dsh/packages/client/ui-fish'
    # Prune generated/dependency directories before walking; do not traverse pnpm links.
    for directory, folders, files in os.walk(ui, followlinks=False):
        folders[:] = [name for name in folders if name not in {'node_modules','.git','.cache'}]
        for name in files:
            path=Path(directory)/name; relative=path.relative_to(ui)
            if path.suffix in {'.map','.tsbuildinfo'}: continue
            if relative.parts[0]=='lib' and relative.as_posix() not in {'lib/index.js','lib/client.js'}: continue
            if path.suffix.lower() in {'.ts','.tsx','.json','.js','.mjs','.css','.md','.svg'}:
                copy(path,DEST/'integrations/dsh/ui-fish'/relative)
    copy(BASE/'设计/实施规格/Fish_统一Agent系统更新_实施规格_2026-10-24.md',DEST/'project/spec.md')
    for path in (BASE/'实施/统一Agent系统更新/工单').glob('*.md'):
        copy(path,DEST/'project/tickets'/path.name)
    (DEST/'README.md').write_text('''# Fish 统一 Agent 更新发布准备

当前源码及原生DSH的Fish前端包；不是生产数据备份，也不是已上传GitHub。

- agent：Fish独立API/人格/工具/经历/记忆/调度/QQ源码。
- integrations/dsh/ui-fish：DSH宿主中的Fish界面，包含已构建包；仍依赖DSH工作区。
- project/spec.md：唯一规格；tickets：同一组工单，不创建第二套需求。

当前运行根保持E盘原部署，M01迁移延期。没有复制vendor、模型缓存、浏览器登录、NapCat账号、DSH个人homes、生产聊天/数据库、截图/日志或旧Git历史。换机仍须准备Python、DSH宿主、QQ/MCP和Fish模型认证。独立语义环境依赖requirements-memory.txt，tokenizer由prepare_context_tokenizer.py准备；这些依赖在当前生产环境已安装。

GitHub连接器已识别用户，但没有创建仓库接口；本机浏览器未登录。独立私有Fish仓库和Issues尚未发布，未写入不相关公开仓库。实际验收原始记录仅留本机实施记录目录。
''',encoding='utf-8')
    (DEST/'agent/docs/agents/execution.md').write_text('# 发布副本入口\n\n唯一规格为../../../project/spec.md，工单为../../../project/tickets/。这是源码发布副本，不是生产数据根。\n',encoding='utf-8')
    manifest={'files':copied,'secret_pattern_files':flagged,'remote_uploaded':False,'old_git_history_included':False,'production_data_included':False}
    (DEST/'发布清单.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'files':len(copied),'flagged_files':flagged,'destination':str(DEST)},ensure_ascii=False))
    return bool(flagged)


if __name__=='__main__': sys.exit(main())
