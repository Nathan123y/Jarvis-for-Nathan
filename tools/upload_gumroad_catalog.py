#!/usr/bin/env python3
"""Create catalog drafts with reviewed assets; publish after buyer testing.

No third-party Python dependencies. Uses the seller's already installed Gumroad
CLI. Checkpoints uncertain writes and never repeats them automatically.
"""
from __future__ import annotations
import argparse
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlsplit


def cli_path():
    local = Path.home() / '.local/bin/gumroad'
    # The installer used by the seller updates this path, even if PATH is stale.
    found = str(local) if local.is_file() else (shutil.which('gumroad') or '')
    if not found:
        raise RuntimeError('Install the Gumroad CLI first, then run ~/.local/bin/gumroad auth login --web.')
    return found


def safe_error_message(value):
    """Show the CLI's error, never a credential or a whole account response."""
    if not isinstance(value, str):
        return ''
    for key in ('GUMROAD_ACCESS_TOKEN', 'GUMROAD_ADMIN_TOKEN'):
        secret = os.environ.get(key)
        if secret:
            value = value.replace(secret, '[redacted]')
    value = re.sub(r'(?i)(bearer\s+)\S+', r'\1[redacted]', value)
    value = re.sub(r'(?i)((?:access_token|refresh_token|client_secret|api_key|token)[\"\x27]?\s*[:=]\s*[\"\x27]?)[^\s\"\x27&,}]+',
                   r'\1[redacted]', value)
    return re.sub(r'[\x00-\x1f\x7f]', ' ', value).strip()[:1200]


def call(cli, args, timeout=300):
    command = ' '.join(args[:2])
    try:
        result = subprocess.run([cli, *args, '--json', '--no-input', '--non-interactive'],
                                capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f'Gumroad {command} timed out. The operation may have completed. Inspect Gumroad before retrying; no automatic retry was made.') from exc
    data = None
    for output in (result.stdout, result.stderr):
        try:
            data = json.loads(output)
            break
        except ValueError:
            continue
    if not isinstance(data, dict):
        message = safe_error_message(result.stderr) or 'The CLI returned no JSON object.'
        raise RuntimeError(f'Gumroad {command} failed (exit {result.returncode}): {message} No definite result; inspect Gumroad before retrying.')
    # Auth status is a local status object, not the API's success envelope.
    # Exit zero can also mean authenticated=false; do not treat that as login.
    auth_status = args[:2] == ['auth', 'status']
    ok = data.get('authenticated') is True if auth_status else data.get('success') is True
    if result.returncode or data.get('success') is False or not ok:
        error = data.get('error')
        if isinstance(error, dict):
            code = safe_error_message(error.get('code')) or 'request_error'
            message = safe_error_message(error.get('message'))
        else:
            code = 'request_error'
            message = safe_error_message(error) or safe_error_message(data.get('message'))
        if auth_status and data.get('authenticated') is False:
            code = 'not_authenticated'
            reason = safe_error_message(data.get('reason')) or 'not_logged_in'
            message = f'Seller login is unavailable ({reason}). Run ~/.local/bin/gumroad auth login --web.'
            if data.get('source') == 'env':
                message += ' GUMROAD_ACCESS_TOKEN overrides the stored login; check that shell setting.'
        message = message or safe_error_message(result.stderr) or 'Unexpected CLI response; no success was confirmed.'
        raise RuntimeError(f'Gumroad {command} failed ({code}): {message} No automatic retry was made.')
    return data


def require_draft_support(cli):
    """An old CLI's default can publish; check --draft before any writes."""
    try:
        result = subprocess.run([cli, 'products', 'create', '--help'],
                                capture_output=True, text=True, timeout=20, check=False)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError('Could not check Gumroad draft support. No product was created.') from exc
    if result.returncode:
        message = safe_error_message(result.stderr) or 'Could not read products create --help.'
        raise RuntimeError(f'Gumroad draft capability check failed: {message} No product was created.')
    if not re.search(r'(?m)^\s*--draft(?:\s|=|$)', result.stdout):
        raise RuntimeError('Your Gumroad CLI lacks --draft. Update it with: curl -fsSL https://gumroad.com/install-cli.sh | bash. Then rerun this uploader. No product was created.')


def product_payload(data):
    return data.get('product') or data.get('result', {}).get('product') or {}


def public_url(product):
    value = product.get('short_url') or product.get('url') or ''
    parsed = urlsplit(str(value))
    if (parsed.scheme != 'https' or not parsed.hostname
            or not (parsed.hostname == 'gumroad.com' or parsed.hostname.endswith('.gumroad.com'))
            or not parsed.path.startswith('/l/') or parsed.username or parsed.password):
        raise RuntimeError('Gumroad did not return a usable public product URL. Check the dashboard; no guessed URL was saved.')
    return value


def save_private(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='catalog-', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def assets(root, product):
    found = {}
    for field in ('buyer_zip', 'cover', 'preview', 'thumbnail'):
        file = (root / product[field]).resolve()
        if not file.is_relative_to(root.resolve()) or not file.is_file():
            raise RuntimeError(f'Missing or invalid {field} for {product["name"]}. Extract the entire seller ZIP first.')
        found[field] = file
    return found


def create_args(root, product):
    files = assets(root, product)
    return ['products', 'create', '--draft', '--name', product['name'], '--type', 'digital',
            '--price', str(product['price']), '--currency', 'usd',
            '--description', product['description'], '--custom-summary', product['subtitle'],
            '--custom-permalink', 'attontios-' + product['id'],
            '--file', str(files['buyer_zip']), '--file-name', files['buyer_zip'].name,
            '--cover-image', str(files['cover']), '--preview-image', str(files['preview']),
            '--thumbnail', str(files['thumbnail'])]


def upload(root, *, create=False, publish=False, tested=False, dry_run=False, cli=None, progress=None):
    if publish and not tested:
        raise RuntimeError('Test checkout and the downloaded buyer files first. Then run with --publish --tested.')
    products = json.loads((root/'products.json').read_text(encoding='utf-8'))
    if not isinstance(products, list) or len(products) != 15 or len({p['id'] for p in products}) != 15:
        raise RuntimeError('Expected this pack\'s 15 unique products. No write was made.')
    # Validate every input before creating any listing.
    commands = [(p, create_args(root, p)) for p in products]
    report=[]
    def emit(message):
        report.append(message)
        if progress is not None:
            progress(message)
    if not (create or publish or dry_run):
        emit('Review the pack, then use --create to upload drafts; --publish --tested after checkout/download tests.')
        return report
    cli = cli or cli_path()
    if create or dry_run:
        require_draft_support(cli)
    if dry_run:
        for product, args in commands:
            call(cli, args+['--dry-run'])
        emit('All 15 create requests passed CLI dry-run. No products were created.')
        return report
    emit('Checking Gumroad seller login...')
    call(cli, ['auth', 'status'], timeout=20)
    state_path=root/'upload_state.json'
    state=json.loads(state_path.read_text()) if state_path.exists() else {}
    listing=call(cli, ['products', 'list', '--all'], timeout=60).get('products')
    if not isinstance(listing, list):
        raise RuntimeError('Could not verify existing products. No create was attempted.')
    for index, (product, args) in enumerate(commands, 1):
        key=product['id']; record=state.get(key, {})
        newly_created=False
        matches=[p for p in listing if isinstance(p,dict) and p.get('name')==product['name']]
        if len(matches)>1:
            raise RuntimeError(f'Duplicate names for {product["name"]}; inspect Gumroad. No duplicate create was attempted.')
        if record.get('status') in {'creating','uncertain','publishing'}:
            raise RuntimeError(f'{product["name"]} has an unresolved operation in upload_state.json. Inspect Gumroad and preserve this checkpoint. Do not repeat uploads blindly.')
        if not record.get('id'):
            if matches:
                raise RuntimeError(f'{product["name"]} already exists but is not tracked in this pack. Verify its buyer content and resolve the checkpoint locally before proceeding. No duplicate was created.')
            if not create:
                raise RuntimeError(f'{product["name"]} has not been created. Run --create first.')
            emit(f'Uploading draft {index}/{len(products)}: {product["name"]}...')
            state[key]={'status':'creating','name':product['name']};save_private(state_path,state)
            try:
                created=product_payload(call(cli,args))
                if not created.get('id') or created.get('name')!=product['name']:
                    raise RuntimeError('Create returned an unexpected identity; inspect Gumroad before retrying.')
                # Preserve the definite identity even if subsequent verification fails.
                state[key].update(id=created['id']);save_private(state_path,state)
                record={'status':'draft','id':created['id'],'name':product['name'],'url':public_url(created)}
                state[key]=record;save_private(state_path,state)
                newly_created=True
            except Exception:
                state[key]['status']='uncertain';save_private(state_path,state);raise
        # Read after each write. A mismatch never leads to an update of another product.
        viewed=product_payload(call(cli,['products','view',str(record['id'])],timeout=60))
        if viewed.get('name')!=product['name'] or str(viewed.get('id'))!=str(record['id']):
            raise RuntimeError('Product identity changed; no publish was attempted.')
        if newly_created and not ((viewed.get('published') is False or viewed.get('is_published') is False)
                                  and viewed.get('published') is not True and viewed.get('is_published') is not True):
            record['status']='uncertain';save_private(state_path,state)
            raise RuntimeError(f'Draft status could not be verified for {product["name"]} ({record["id"]}). Inspect Gumroad and preserve upload_state.json. No further product was created.')
        if publish and viewed.get('published') is not True and viewed.get('is_published') is not True:
            emit(f'Publishing tested product {index}/{len(products)}: {product["name"]}...')
            record['status']='publishing';save_private(state_path,state)
            try:
                call(cli,['products','publish',str(record['id'])],timeout=60)
                viewed=product_payload(call(cli,['products','view',str(record['id'])],timeout=60))
            except Exception:
                record['status']='uncertain';save_private(state_path,state);raise
        published=viewed.get('published') is True or viewed.get('is_published') is True
        if publish and not published:
            record['status']='uncertain';save_private(state_path,state)
            raise RuntimeError('Publication could not be verified. Inspect Gumroad; no automatic retry will run.')
        record.update(status='published' if published else 'draft',url=public_url(viewed));save_private(state_path,state)
        emit(f"{record['status']}: {product['name']} - {record['url']}")
    live={key:{'url':record['url'],'published':True} for key,record in state.items() if record.get('status')=='published'}
    project=Path.home()/'Projects/Jarvis-for-Nathan'
    if live and project.is_dir():
        links=project/'config/product_sales/catalog_links.json'
        previous=json.loads(links.read_text()) if links.exists() else {}
        previous.update(live);save_private(links,previous)
        emit('Published links connected to Jarvis. Reopen it and ask "Show my products."')
    emit('Created drafts remain unpublished until tested and published. No outreach was sent.')
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('pack',nargs='?',type=Path,default=Path(__file__).resolve().parent)
    parser.add_argument('--create',action='store_true')
    parser.add_argument('--publish',action='store_true')
    parser.add_argument('--tested',action='store_true',help='You checked checkout/download and opened each buyer bundle.')
    parser.add_argument('--dry-run',action='store_true')
    args=parser.parse_args()
    try:
        upload(args.pack.resolve(),create=args.create,publish=args.publish,tested=args.tested,dry_run=args.dry_run,
               progress=lambda line: print(line, flush=True))
    except (RuntimeError,OSError,ValueError) as exc:
        parser.exit(1,str(exc)+'\n')

if __name__=='__main__':main()
