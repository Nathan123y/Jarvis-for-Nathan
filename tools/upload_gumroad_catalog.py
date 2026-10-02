#!/usr/bin/env python3
"""Create catalog drafts with reviewed assets; publish after buyer testing.

No third-party Python dependencies. Uses the seller's already installed Gumroad
CLI. Checkpoints uncertain writes and never repeats them automatically.
"""
from __future__ import annotations
import argparse
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlsplit


def cli_path():
    local = Path.home() / '.local/bin/gumroad'
    found = shutil.which('gumroad') or (str(local) if local.is_file() else '')
    if not found:
        raise RuntimeError('Install the Gumroad CLI first, then run ~/.local/bin/gumroad auth login --web.')
    return found


def call(cli, args, timeout=300):
    try:
        result = subprocess.run([cli, *args, '--json', '--no-input', '--non-interactive'],
                                capture_output=True, text=True, timeout=timeout, check=False)
        data = json.loads(result.stdout)
    except (subprocess.TimeoutExpired, ValueError) as exc:
        raise RuntimeError('No definite CLI result. The operation may have completed. Inspect Gumroad before retrying; no automatic retry was made.') from exc
    if result.returncode or not isinstance(data, dict) or data.get('success') is not True:
        code = data.get('error', {}).get('code', 'unknown_error') if isinstance(data, dict) else 'invalid_response'
        raise RuntimeError(f'Gumroad request failed ({code}). Inspect the dashboard and CLI output locally. For login errors run ~/.local/bin/gumroad auth login --web. No automatic retry was made.')
    return data


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
    return ['products', 'create', '--name', product['name'], '--type', 'digital',
            '--price', str(product['price']), '--currency', 'usd',
            '--description', product['description'], '--custom-summary', product['subtitle'],
            '--custom-permalink', 'attontios-' + product['id'],
            '--file', str(files['buyer_zip']), '--file-name', files['buyer_zip'].name,
            '--cover-image', str(files['cover']), '--preview-image', str(files['preview']),
            '--thumbnail', str(files['thumbnail'])]


def upload(root, *, create=False, publish=False, tested=False, dry_run=False, cli=None):
    if publish and not tested:
        raise RuntimeError('Test checkout and the downloaded buyer files first. Then run with --publish --tested.')
    products = json.loads((root/'products.json').read_text(encoding='utf-8'))
    if not isinstance(products, list) or len(products) != 15 or len({p['id'] for p in products}) != 15:
        raise RuntimeError('Expected this pack\'s 15 unique products. No write was made.')
    # Validate every input before creating any listing.
    commands = [(p, create_args(root, p)) for p in products]
    if not (create or publish or dry_run):
        return ['Review the pack, then use --create to upload drafts; --publish --tested after checkout/download tests.']
    cli = cli or cli_path()
    if dry_run:
        for product, args in commands:
            call(cli, args+['--dry-run'])
        return ['All 15 create requests passed CLI dry-run. No products were created.']
    call(cli, ['auth', 'status'], timeout=20)
    state_path=root/'upload_state.json'
    state=json.loads(state_path.read_text()) if state_path.exists() else {}
    listing=call(cli, ['products', 'list', '--all'], timeout=60).get('products')
    if not isinstance(listing, list):
        raise RuntimeError('Could not verify existing products. No create was attempted.')
    report=[]
    for product, args in commands:
        key=product['id']; record=state.get(key, {})
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
            state[key]={'status':'creating','name':product['name']};save_private(state_path,state)
            try:
                created=product_payload(call(cli,args))
                if not created.get('id') or created.get('name')!=product['name']:
                    raise RuntimeError('Create returned an unexpected identity; inspect Gumroad before retrying.')
                record={'status':'draft','id':created['id'],'name':product['name'],'url':public_url(created)}
                state[key]=record;save_private(state_path,state)
            except Exception:
                state[key]['status']='uncertain';save_private(state_path,state);raise
            report.append('Draft uploaded: '+product['name'])
        # Read after each write. A mismatch never leads to an update of another product.
        viewed=product_payload(call(cli,['products','view',str(record['id'])],timeout=60))
        if viewed.get('name')!=product['name'] or str(viewed.get('id'))!=str(record['id']):
            raise RuntimeError('Product identity changed; no publish was attempted.')
        if publish and viewed.get('published') is not True and viewed.get('is_published') is not True:
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
        report.append(f"{record['status']}: {product['name']} - {record['url']}")
    live={key:{'url':record['url'],'published':True} for key,record in state.items() if record.get('status')=='published'}
    project=Path.home()/'Projects/Jarvis-for-Nathan'
    if live and project.is_dir():
        links=project/'config/product_sales/catalog_links.json'
        previous=json.loads(links.read_text()) if links.exists() else {}
        previous.update(live);save_private(links,previous)
        report.append('Published links connected to Jarvis. Reopen it and ask "Show my products."')
    report.append('Created drafts remain unpublished until tested and published. No outreach was sent.')
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
        for line in upload(args.pack.resolve(),create=args.create,publish=args.publish,tested=args.tested,dry_run=args.dry_run):print(line)
    except (RuntimeError,OSError,ValueError) as exc:
        parser.exit(1,str(exc)+'\n')

if __name__=='__main__':main()
