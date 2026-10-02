"""Catalog uploads must never duplicate uncertain writes or publish untested files."""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from tools import upload_gumroad_catalog as uploader

class UploadTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory();self.root=Path(self.directory.name)
        self.products=[dict(id=f'p{i}',name=f'Product {i}',price='12.00',description='Files',subtitle='Organizer',buyer_zip=f'{i}.zip',cover=f'{i}.png',preview=f'{i}.png',thumbnail=f'{i}.png') for i in range(15)]
        (self.root/'products.json').write_text(json.dumps(self.products))
        for p in self.products:
            (self.root/p['buyer_zip']).write_bytes(b'zip');(self.root/p['cover']).write_bytes(b'png')
        self.real_draft_check=uploader.require_draft_support
        self.draft_patcher=patch.object(uploader,'require_draft_support')
        self.draft_check=self.draft_patcher.start()
        self.addCleanup(self.draft_patcher.stop)
    def tearDown(self):self.directory.cleanup()
    def test_all_assets_checked_before_first_write(self):
        (self.root/'14.zip').unlink()
        with patch.object(uploader,'call') as call:
            with self.assertRaisesRegex(RuntimeError,'Missing'):uploader.upload(self.root,create=True,cli='fake')
            call.assert_not_called()
    def test_uncertain_create_is_checkpointed_and_not_repeated(self):
        def call(cli,args,**kwargs):
            if args[:2]==['auth','status']:return {'authenticated':True}
            if args[:2]==['products','list']:return {'products':[]}
            raise RuntimeError('Unknown write result')
        with patch.object(uploader,'call',side_effect=call):
            with self.assertRaisesRegex(RuntimeError,'Unknown'):uploader.upload(self.root,create=True,cli='fake')
        self.assertEqual(json.loads((self.root/'upload_state.json').read_text())['p0']['status'],'uncertain')
        with patch.object(uploader,'call',side_effect=call) as mock:
            with self.assertRaisesRegex(RuntimeError,'unresolved'):uploader.upload(self.root,create=True,cli='fake')
            self.assertTrue(all(c.args[1][:2]!=['products','create'] for c in mock.call_args_list))
    def test_publish_requires_buyer_testing_before_any_command(self):
        with patch.object(uploader,'call') as call:
            with self.assertRaisesRegex(RuntimeError,'Test checkout'):uploader.upload(self.root,publish=True,cli='fake')
            call.assert_not_called()
    def test_existing_untracked_listing_is_not_duplicated(self):
        with patch.object(uploader,'call',side_effect=[{'authenticated':True},{'products':[{'id':'EX','name':'Product 0'}]}]) as call:
            with self.assertRaisesRegex(RuntimeError,'already exists'):uploader.upload(self.root,create=True,cli='fake')
            self.assertEqual(call.call_count,2)
    def test_description_and_download_are_discrete_cli_arguments(self):
        p=self.products[0];p['description']='line 1\nline 2 $(no shell)'
        args=uploader.create_args(self.root,p)
        self.assertEqual(args[args.index('--description')+1],p['description'])
        self.assertEqual(args[args.index('--file')+1],str(self.root/'0.zip'))
        self.assertEqual(args[args.index('--price')+1],'12.00')
        self.assertIn('--draft',args)
        self.assertEqual(uploader.product_payload({'result':{'product':{'id':'1'}}}),{'id':'1'})

    def test_real_cli_status_format_creates_all_15_verified_drafts(self):
        remote={};commands=[];progress=[]
        def run(args,**kwargs):
            commands.append(args)
            if args[1:]==['products','create','--help']:
                return subprocess.CompletedProcess(args,0,'Flags:\n      --draft   Save as an unpublished draft\n','')
            if args[1:3]==['auth','status']:
                data={'authenticated':True,'user':{'name':'Example Seller'},'source':'config'}
            elif args[1:3]==['products','list']:
                data={'success':True,'products':list(remote.values())}
            elif args[1:3]==['products','create']:
                self.assertIn('--draft',args)
                name=args[args.index('--name')+1];key='ID'+str(len(remote))
                remote[key]={'id':key,'name':name,'published':False,'short_url':'https://example.gumroad.com/l/'+key}
                data={'success':True,'product':remote[key]}
            elif args[1:3]==['products','view']:
                data={'success':True,'product':remote[args[3]]}
            else:
                self.fail('Unexpected CLI command: '+str(args[:3]))
            return subprocess.CompletedProcess(args,0,json.dumps(data),'')
        self.draft_check.side_effect=self.real_draft_check
        with patch.object(uploader.subprocess,'run',side_effect=run):
            report=uploader.upload(self.root,create=True,cli='fake',progress=progress.append)
        self.assertEqual(len(remote),15)
        self.assertEqual(report,progress)
        self.assertEqual(sum(line.startswith('draft:') for line in progress),15)
        state=json.loads((self.root/'upload_state.json').read_text())
        self.assertEqual({p['status'] for p in state.values()},{'draft'})
        self.assertEqual(len(state),15)
        self.assertFalse(any(c[1:3]==['products','publish'] for c in commands))

    def test_unauthenticated_seller_stops_before_any_create(self):
        response=subprocess.CompletedProcess([],0,json.dumps({'authenticated':False,'reason':'invalid_or_expired'}),'')
        with patch.object(uploader.subprocess,'run',return_value=response) as run:
            with self.assertRaisesRegex(RuntimeError,'not_authenticated.*invalid_or_expired'):
                uploader.upload(self.root,create=True,cli='fake')
        self.assertEqual(run.call_count,1)
        self.assertFalse((self.root/'upload_state.json').exists())

    def test_old_cli_is_blocked_before_any_product_write(self):
        self.draft_check.side_effect=self.real_draft_check
        response=subprocess.CompletedProcess([],0,'Flags:\n      --price string   Price\n','')
        with patch.object(uploader.subprocess,'run',return_value=response) as run:
            with self.assertRaisesRegex(RuntimeError,'lacks --draft'):
                uploader.upload(self.root,create=True,cli='fake')
        self.assertEqual(run.call_args.args[0],['fake','products','create','--help'])
        self.assertEqual(run.call_count,1)
        self.assertFalse((self.root/'upload_state.json').exists())

    def test_unexpected_live_create_preserves_id_and_blocks_next_product(self):
        product={'id':'KNOWN','name':'Product 0','published':True,'short_url':'https://example.gumroad.com/l/known'}
        with patch.object(uploader,'call',side_effect=[{'authenticated':True},{'products':[]},{'product':product},{'product':product}]) as call:
            with self.assertRaisesRegex(RuntimeError,'Draft status could not be verified'):
                uploader.upload(self.root,create=True,cli='fake')
        state=json.loads((self.root/'upload_state.json').read_text())
        self.assertEqual(state['p0']['id'],'KNOWN')
        self.assertEqual(state['p0']['status'],'uncertain')
        self.assertEqual(len(state),1)
        self.assertEqual(sum(c.args[1][:2]==['products','create'] for c in call.call_args_list),1)

    def test_no_action_explains_next_step_without_calling_cli(self):
        progress=[]
        with patch.object(uploader,'call') as call:
            report=uploader.upload(self.root,progress=progress.append)
        self.assertEqual(report,progress)
        self.assertIn('--create',progress[0])
        call.assert_not_called()


class CLIResponseTests(unittest.TestCase):
    def response(self,data,*,code=0,stderr=''):
        return subprocess.CompletedProcess([],code,json.dumps(data),stderr)

    def test_authenticated_object_without_success_is_accepted(self):
        data={'authenticated':True,'user':{'name':'Example Seller'},'source':'config'}
        with patch.object(uploader.subprocess,'run',return_value=self.response(data)):
            self.assertEqual(uploader.call('fake',['auth','status']),data)

    def test_false_login_blocks_even_when_admin_is_authenticated(self):
        data={'authenticated':False,'reason':'not_logged_in','admin':{'authenticated':True}}
        with patch.object(uploader.subprocess,'run',return_value=self.response(data)):
            with self.assertRaisesRegex(RuntimeError,'not_authenticated.*auth login --web'):
                uploader.call('fake',['auth','status'])

    def test_nonzero_exit_never_counts_as_authenticated(self):
        with patch.object(uploader.subprocess,'run',return_value=self.response({'authenticated':True},code=1)):
            with self.assertRaisesRegex(RuntimeError,'auth status failed'):
                uploader.call('fake',['auth','status'])

    def test_product_command_still_requires_explicit_success(self):
        with patch.object(uploader.subprocess,'run',return_value=self.response({'product':{'id':'UNKNOWN'}})):
            with self.assertRaisesRegex(RuntimeError,'no success was confirmed'):
                uploader.call('fake',['products','create'])

    def test_api_error_names_command_code_and_message(self):
        data={'success':False,'error':{'code':'permission_denied','message':'This account cannot create products.'}}
        with patch.object(uploader.subprocess,'run',return_value=self.response(data,code=1)):
            with self.assertRaisesRegex(RuntimeError,'products create failed \\(permission_denied\\).*cannot create products'):
                uploader.call('fake',['products','create'])

    def test_plain_stderr_is_visible_without_secret_values(self):
        error='unknown flag: --draft; token=othersecret; Authorization: Bearer anothersecret; seller-secret'
        response=subprocess.CompletedProcess([],1,'',error)
        with patch.dict(os.environ,{'GUMROAD_ACCESS_TOKEN':'seller-secret'}),patch.object(uploader.subprocess,'run',return_value=response):
            with self.assertRaisesRegex(RuntimeError,'unknown flag: --draft') as caught:
                uploader.call('fake',['products','create'])
        message=str(caught.exception)
        for secret in ('seller-secret','anothersecret','othersecret'):
            self.assertNotIn(secret,message)

    def test_json_error_on_stderr_is_reported(self):
        response=subprocess.CompletedProcess([],1,'',json.dumps({'success':False,'error':{'code':'usage_error','message':'Unknown flag --draft'}}))
        with patch.object(uploader.subprocess,'run',return_value=response):
            with self.assertRaisesRegex(RuntimeError,'usage_error.*Unknown flag --draft'):
                uploader.call('fake',['products','create'])

    def test_timeout_keeps_write_uncertain(self):
        with patch.object(uploader.subprocess,'run',side_effect=subprocess.TimeoutExpired('fake',3)):
            with self.assertRaisesRegex(RuntimeError,'products create timed out.*may have completed'):
                uploader.call('fake',['products','create'])

if __name__=='__main__':unittest.main()
