"""Catalog uploads must never duplicate uncertain writes or publish untested files."""
import json
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
    def tearDown(self):self.directory.cleanup()
    def test_all_assets_checked_before_first_write(self):
        (self.root/'14.zip').unlink()
        with patch.object(uploader,'call') as call:
            with self.assertRaisesRegex(RuntimeError,'Missing'):uploader.upload(self.root,create=True,cli='fake')
            call.assert_not_called()
    def test_uncertain_create_is_checkpointed_and_not_repeated(self):
        def call(cli,args,**kwargs):
            if args[:2]==['auth','status']:return {'success':True}
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
        with patch.object(uploader,'call',side_effect=[{'success':True},{'products':[{'id':'EX','name':'Product 0'}]}]) as call:
            with self.assertRaisesRegex(RuntimeError,'already exists'):uploader.upload(self.root,create=True,cli='fake')
            self.assertEqual(call.call_count,2)
    def test_description_and_download_are_discrete_cli_arguments(self):
        p=self.products[0];p['description']='line 1\nline 2 $(no shell)'
        args=uploader.create_args(self.root,p)
        self.assertEqual(args[args.index('--description')+1],p['description'])
        self.assertEqual(args[args.index('--file')+1],str(self.root/'0.zip'))
        self.assertEqual(args[args.index('--price')+1],'12.00')
        self.assertEqual(uploader.product_payload({'result':{'product':{'id':'1'}}}),{'id':'1'})

if __name__=='__main__':unittest.main()
