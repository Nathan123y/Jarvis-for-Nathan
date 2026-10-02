"""Product selection, publication checks and draft snapshots are authoritative."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from plugins import product_sales as sales

class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.links=Path(self.directory.name)/'catalog_links.json'
        self.state=Path(self.directory.name)/'state.json'
        self.patches=[patch.object(sales,'_LINKS_FILE',self.links),patch.object(sales,'_STATE_FILE',self.state)]
        for p in self.patches:p.start()
    def tearDown(self):
        for p in self.patches:p.stop()
        self.directory.cleanup()
    def test_sixteen_downloads_plus_website_service_and_unpublished_pitch_gate(self):
        self.assertEqual(len(sales._catalog()),17)
        self.assertEqual(len({p['id'] for p in sales._catalog()}),17)
        self.assertEqual(len([p for p in sales._catalog() if p.get('kind') != 'service']),16)
        self.assertIn('100 prepared rows',sales.run({'action':'brief','product':'tutor-sessions'}))
        text=sales.run({'action':'campaign','product':'tutor-sessions'})
        self.assertIn('no verified published',text)
        self.assertFalse(self.state.exists())
        self.assertIn('Choose one exact',sales.run({'action':'brief','product':'tutor'}))
    def test_sync_requires_published_exact_name_and_unique_match(self):
        p=sales._product({'product':'tutor-sessions'})
        records=[{'name':p['name'],'published':False,'short_url':'https://example.gumroad.com/l/tutor'},
                 {'name':'Cleaning Business Job Organizer','published':True,'short_url':'https://example.gumroad.com/l/clean'},
                 {'name':'Pet Care Booking Organizer','published':True,'short_url':'https://evil.example/l/pet'},
                 {'name':'Music Lesson Studio Organizer','published':True,'short_url':'https://example.gumroad.com/l/m1'},
                 {'name':'Music Lesson Studio Organizer','published':True,'short_url':'https://example.gumroad.com/l/m2'}]
        with patch.object(sales,'_cli_json',return_value={'products':records}):
            text=sales.run({'action':'sync'})
        self.assertIn('Connected 1',text)
        self.assertIn('duplicate product names',text)
        self.assertEqual(sales._product({'product':'tutor-sessions'})['url'],'')
        self.assertEqual(sales._product({'product':'cleaning-jobs'})['url'],'https://example.gumroad.com/l/clean')
        self.assertEqual(self.links.stat().st_mode&0o777,0o600)
    def test_draft_retains_chosen_product_when_links_change(self):
        sales._write_links({'tutor-sessions':{'published':True,'url':'https://example.gumroad.com/l/tutor'}})
        sales.run({'action':'add_lead','name':'Example Tutor','email':'tutor@example.com','context':'Tutoring business','relationship':'Known to the user'})
        result=sales.run({'action':'draft','product':'Tutor Session Organizer','email':'tutor@example.com'})
        self.assertIn('Tutor Session Organizer',result)
        draft=next(iter(sales._load()['drafts'].values()))
        self.assertEqual(draft['product_id'],'tutor-sessions')
        sales._write_links({'tutor-sessions':{'published':True,'url':'https://example.gumroad.com/l/new'}})
        shown=sales.run({'action':'show','draft_id':draft['id'],'product':'cleaning-jobs'})
        self.assertIn('/l/tutor?',shown)
        self.assertNotIn('/l/new',shown)
        self.assertNotIn('Soccer Coach',shown)
    def test_cross_product_switch_does_not_reset_opt_out(self):
        sales._write_links({'tutor-sessions':{'published':True,'url':'https://example.gumroad.com/l/tutor'}})
        sales.run({'action':'add_lead','name':'Example Tutor','email':'tutor@example.com','context':'Tutoring','relationship':'Known'})
        sales.run({'action':'record','email':'tutor@example.com','outcome':'do_not_contact'})
        text=sales.run({'action':'draft','product':'tutor-sessions','email':'tutor@example.com'})
        self.assertIn('opted out',text)
        self.assertFalse(sales._load()['drafts'])
    def test_sales_uses_selected_product(self):
        sales._write_links({'tutor-sessions':{'published':True,'url':'https://example.gumroad.com/l/tutor'}})
        with patch.object(sales,'_cli_json',side_effect=[{'product':{'id':'TID'}},{'sales':[]}]) as cli:
            text=sales.run({'action':'sales','product':'tutor-sessions'})
        self.assertEqual(cli.call_args_list[0].args[0],['products','view','tutor'])
        self.assertIn('TID',cli.call_args_list[1].args[0])
        self.assertIn('0 reported orders',text)

    def test_missing_list_url_is_resolved_by_verified_product_id(self):
        listing={'id':'TID','name':'Tutor Session Organizer','published':True}
        detail=dict(listing,short_url='https://example.gumroad.com/l/tutor')
        with patch.object(sales,'_cli_json',side_effect=[{'products':[listing]},{'product':detail}]) as cli:
            text=sales.run({'action':'sync'})
        self.assertIn('Connected 1',text)
        self.assertEqual(cli.call_args_list[1].args[0],['products','view','TID'])
        self.assertEqual(sales._product({'product':'tutor-sessions'})['url'],detail['short_url'])

    def test_missing_status_is_verified_and_draft_is_not_connected(self):
        listing={'id':'TID','name':'Tutor Session Organizer','short_url':'https://example.gumroad.com/l/tutor'}
        detail=dict(listing,published=False)
        with patch.object(sales,'_cli_json',side_effect=[{'products':[listing]},{'result':{'product':detail}}]):
            text=sales.run({'action':'sync'})
        self.assertIn('Connected 0',text)
        self.assertIn('Not published yet: Tutor Session Organizer',text)
        self.assertEqual(sales._product({'product':'tutor-sessions'})['url'],'')

    def test_manual_name_capitalization_and_spaces_match_without_guessing(self):
        listing={'name':'  TUTOR   session Organizer  ','published':True,'short_url':'https://example.gumroad.com/l/tutor'}
        with patch.object(sales,'_cli_json',return_value={'products':[listing]}) as cli:
            text=sales.run({'action':'sync'})
        self.assertIn('Connected 1',text)
        self.assertEqual(cli.call_count,1)

    def test_normalized_duplicate_names_remain_ambiguous(self):
        records=[{'name':name,'published':True,'short_url':'https://example.gumroad.com/l/'+str(i)}
                 for i,name in enumerate(['Tutor Session Organizer',' TUTOR  session organizer '])]
        with patch.object(sales,'_cli_json',return_value={'products':records}):
            text=sales.run({'action':'sync'})
        self.assertIn('Connected 0',text)
        self.assertIn('Resolve duplicate product names: Tutor Session Organizer',text)
        self.assertEqual(sales._product({'product':'tutor-sessions'})['url'],'')

    def test_changed_wording_reports_observed_names_and_successful_access(self):
        records=[{'name':'My Tutor Planner','published':True,'short_url':'https://example.gumroad.com/l/tutor'}]
        with patch.object(sales,'_cli_json',return_value={'products':records}):
            text=sales.run({'action':'sync'})
        self.assertIn('Connected 0',text)
        self.assertIn('Gumroad returned 1 seller listings successfully',text)
        self.assertIn('No matching catalog name:',text)
        self.assertIn('My Tutor Planner [published]',text)
        self.assertNotIn('API key',text)
        self.assertEqual(sales._product({'product':'tutor-sessions'})['url'],'')

    def test_zero_due_to_drafts_is_explained_and_shown_on_screen(self):
        records=[{'name':'Tutor Session Organizer','published':False}]
        player=MagicMock()
        with patch.object(sales,'_cli_json',return_value={'products':records}):
            text=sales.run({'action':'sync'},player=player)
        self.assertIn('Not published yet: Tutor Session Organizer',text)
        self.assertIn('Tutor Session Organizer [draft]',text)
        player.show_content.assert_called_once_with('GUMROAD CATALOG SYNC',text[:3900])

    def test_failed_detail_lookup_preserves_existing_bindings(self):
        sales._write_links({'cleaning-jobs':{'published':True,'url':'https://example.gumroad.com/l/clean'}})
        before=self.links.read_bytes()
        records=[{'id':'TID','name':'Tutor Session Organizer','published':True}]
        with patch.object(sales,'_cli_json',side_effect=[{'products':records},RuntimeError('Detail lookup unavailable')]):
            text=sales.run({'action':'sync'})
        self.assertIn('Existing links were preserved',text)
        self.assertNotIn('Connected 0',text)
        self.assertEqual(self.links.read_bytes(),before)

    def test_detail_lookup_cannot_bind_a_different_product(self):
        sales._write_links({'cleaning-jobs':{'published':True,'url':'https://example.gumroad.com/l/clean'}})
        before=self.links.read_bytes()
        record={'id':'TID','name':'Tutor Session Organizer','published':True}
        wrong={'id':'OTHER','name':'Tutor Session Organizer','published':True,'short_url':'https://example.gumroad.com/l/other'}
        with patch.object(sales,'_cli_json',side_effect=[{'products':[record]},{'product':wrong}]):
            text=sales.run({'action':'sync'})
        self.assertIn('did not match',text)
        self.assertEqual(self.links.read_bytes(),before)

    def test_malformed_catalog_does_not_replace_links(self):
        sales._write_links({'cleaning-jobs':{'published':True,'url':'https://example.gumroad.com/l/clean'}})
        before=self.links.read_bytes()
        with patch.object(sales,'_cli_json',return_value={'products':[None]}):
            text=sales.run({'action':'sync'})
        self.assertIn('unexpected catalog',text)
        self.assertEqual(self.links.read_bytes(),before)

    def test_conflicting_publication_flags_require_verification(self):
        record={'name':'Tutor Session Organizer','published':True,'is_published':False,
                'short_url':'https://example.gumroad.com/l/tutor'}
        with patch.object(sales,'_cli_json',return_value={'products':[record]}):
            text=sales.run({'action':'sync'})
        self.assertIn('Connected 0',text)
        self.assertIn('published status missing',text)
        self.assertEqual(sales._product({'product':'tutor-sessions'})['url'],'')

    def photography_detail(self, **changes):
        return dict({'id':'PHOTO-ID','name':'Photographer Bookings','published':True,
                     'short_url':'https://example.gumroad.com/l/photobook'}, **changes)

    def connect_photography(self, detail=None, **args):
        with patch.object(sales,'_cli_json',return_value={'product':detail or self.photography_detail()}) as cli:
            text=sales.run(dict({'action':'link','product':'photographer-bookings',
                                 'product_url':'https://example.gumroad.com/l/photobook'}, **args))
        return text, cli

    def test_connect_renamed_listing_keeps_other_links_and_enables_correct_pitch(self):
        sales._write_links({'tutor-sessions':{'published':True,'url':'https://example.gumroad.com/l/tutor'}})
        player=MagicMock()
        with patch.object(sales,'_cli_json',return_value={'product':self.photography_detail()}) as cli:
            text=sales.run({'action':'link','product':'photographer-bookings',
                            'product_url':'https://example.gumroad.com/l/photobook?utm_source=old#top'},player=player)
        cli.assert_called_once_with(['products','view','photobook'])
        self.assertIn('Connected Photographer Booking Organizer',text)
        self.assertIn('Photographer Bookings',text)
        self.assertFalse(self.state.exists())
        self.assertEqual(sales._product({'product':'tutor-sessions'})['url'],'https://example.gumroad.com/l/tutor')
        self.assertEqual(self.links.stat().st_mode & 0o777,0o600)
        player.show_content.assert_called_once_with('GUMROAD PRODUCT CONNECTED',text)
        self.assertIn('Gumroad title: Photographer Bookings',sales.run({'action':'products'}))
        brief=sales.run({'action':'brief','product':'  PHOTOGRAPHER   BOOKINGS '})
        self.assertIn('Shoot Plan',brief)
        self.assertIn('https://example.gumroad.com/l/photobook',brief)
        sales.run({'action':'add_lead','name':'Example Studio','email':'studio@example.com',
                   'relationship':'Known photographer','context':'Portrait photography business'})
        text=sales.run({'action':'draft','product':'Photographer Bookings','email':'studio@example.com'})
        self.assertIn('/l/photobook?',text)
        self.assertNotIn('/l/kvaya',text)
        self.assertNotIn('/l/tutor',text)

    def test_link_requires_explicit_catalog_product_and_valid_public_url_before_cli(self):
        invalid=[{'product_url':'https://example.gumroad.com/l/photobook'},
                 {'product':'unknown-product','product_url':'https://example.gumroad.com/l/photobook'}]
        invalid += [{'product':'photographer-bookings','product_url':url} for url in
                    ['http://example.gumroad.com/l/p','https://evil.example/l/p',
                     'https://example.gumroad.com.evil.example/l/p','https://user@example.gumroad.com/l/p',
                     'https://example.gumroad.com:1234/l/p','https://example.gumroad.com/l/',
                     'https://example.gumroad.com/l/p/edit','https://example.gumroad.com/l/p%2Fedit']]
        with patch.object(sales,'_cli_json') as cli:
            for args in invalid:
                with self.subTest(args=args):
                    self.assertNotIn('Connected ',sales.run(dict(action='link',**args)))
            cli.assert_not_called()
        self.assertFalse(self.links.exists())

    def test_direct_link_never_accepts_draft_missing_or_conflicting_publication(self):
        sales._write_links({'tutor-sessions':{'published':True,'url':'https://example.gumroad.com/l/tutor'}})
        before=self.links.read_bytes()
        for state in [{'published':False},{'published':None},{'published':True,'is_published':False}]:
            with self.subTest(state=state):
                text,_=self.connect_photography(self.photography_detail(**state))
                self.assertIn('not verified as published',text)
                self.assertEqual(self.links.read_bytes(),before)

    def test_direct_link_rejects_wrong_identity_or_public_url_and_preserves_bindings(self):
        sales._write_links({'tutor-sessions':{'published':True,'url':'https://example.gumroad.com/l/tutor'}})
        before=self.links.read_bytes()
        for changes in [{'id':None},{'name':None},{'short_url':''},
                        {'short_url':'https://example.gumroad.com/l/different'},
                        {'short_url':'https://another.gumroad.com/l/photobook'},
                        {'short_url':'https://evil.example/l/photobook'}]:
            with self.subTest(changes=changes):
                text,_=self.connect_photography(self.photography_detail(**changes))
                self.assertNotIn('Connected ',text)
                self.assertEqual(self.links.read_bytes(),before)

    def test_direct_link_accepts_legacy_detail_wrapper_and_generic_gumroad_alias(self):
        with patch.object(sales,'_cli_json',return_value={'result':{'product':self.photography_detail()}}):
            text=sales.run({'action':'link','product':'photographer-bookings',
                            'product_url':'https://gumroad.com/l/photobook'})
        self.assertIn('Connected Photographer Booking Organizer',text)
        self.assertEqual(sales._product({'product':'photographer-bookings'})['url'],self.photography_detail()['short_url'])

    def test_link_failure_never_replaces_links_or_reports_success(self):
        sales._write_links({'tutor-sessions':{'published':True,'url':'https://example.gumroad.com/l/tutor'}})
        before=self.links.read_bytes()
        with patch.object(sales,'_cli_json',side_effect=RuntimeError('Seller login unavailable')):
            text=sales.run({'action':'link','product':'photographer-bookings',
                            'product_url':'https://example.gumroad.com/l/photobook'})
        self.assertEqual(text,'Seller login unavailable')
        self.assertEqual(self.links.read_bytes(),before)

    def test_one_gumroad_listing_cannot_be_connected_to_two_different_bundles(self):
        self.connect_photography()
        before=self.links.read_bytes()
        text,_=self.connect_photography(product='tutor-sessions')
        self.assertIn('already mapped to another catalog product',text)
        self.assertEqual(self.links.read_bytes(),before)

    def test_duplicate_gumroad_titles_require_selection_by_catalog_id(self):
        self.connect_photography()
        detail={'id':'TUTOR-ID','name':'Photographer Bookings','published':True,
                'short_url':'https://example.gumroad.com/l/tutor'}
        with patch.object(sales,'_cli_json',return_value={'product':detail}):
            sales.run({'action':'link','product':'tutor-sessions','product_url':detail['short_url']})
        self.assertIn('Choose one exact',sales.run({'action':'brief','product':'Photographer Bookings'}))
        self.assertIn('Shoot Plan',sales.run({'action':'brief','product':'photographer-bookings'}))

    def test_gumroad_title_cannot_take_over_another_catalog_id_or_default_product(self):
        self.connect_photography(self.photography_detail(name='soccer-coach'))
        self.assertEqual(sales._product({})['id'],'soccer-coach')
        self.assertEqual(sales._product({'product':'kvaya'})['id'],'soccer-coach')
        self.assertEqual(sales._product({'product':'photographer-bookings'})['id'],'photographer-bookings')

    def test_legacy_binding_without_id_cannot_duplicate_the_same_product_url(self):
        sales._write_links({'tutor-sessions':{'published':True,'url':self.photography_detail()['short_url']}})
        before=self.links.read_bytes()
        text,_=self.connect_photography()
        self.assertIn('already mapped to another catalog product',text)
        self.assertEqual(self.links.read_bytes(),before)

    def test_sync_preserves_explicit_mapping_after_further_title_and_url_changes(self):
        self.connect_photography()
        detail=self.photography_detail(name='Studio Admin Kit',short_url='https://example.gumroad.com/l/studio')
        # A same-catalog-name listing cannot take over the deliberately mapped ID.
        distractor=self.photography_detail(id='WRONG-ID',name='Photographer Booking Organizer',
                                            short_url='https://example.gumroad.com/l/wrong')
        with patch.object(sales,'_cli_json',return_value={'products':[detail,distractor]}):
            text=sales.run({'action':'sync'})
        self.assertIn('Connected 1',text)
        self.assertEqual(sales._product({'product':'Studio Admin Kit'})['url'],detail['short_url'])
        self.assertEqual(json.loads(self.links.read_text())['photographer-bookings']['mapping'],'explicit')

    def test_sync_disables_unpublished_mapped_product_and_recovers_after_republish(self):
        self.connect_photography()
        with patch.object(sales,'_cli_json',return_value={'products':[self.photography_detail(published=False)]}):
            text=sales.run({'action':'sync'})
        self.assertIn('Connected 0',text)
        self.assertIn('Not published yet: Photographer Booking Organizer',text)
        self.assertIn('no verified published',sales.run({'action':'campaign','product':'Photographer Bookings'}))
        self.assertEqual(json.loads(self.links.read_text())['photographer-bookings']['gumroad_id'],'PHOTO-ID')
        with patch.object(sales,'_cli_json',return_value={'products':[self.photography_detail()]}):
            self.assertIn('Connected 1',sales.run({'action':'sync'}))
        self.assertEqual(sales._product({'product':'Photographer Bookings'})['url'],self.photography_detail()['short_url'])

    def test_missing_mapped_id_is_disabled_without_falling_back_to_name(self):
        self.connect_photography()
        wrong=self.photography_detail(id='WRONG-ID',name='Photographer Booking Organizer')
        with patch.object(sales,'_cli_json',return_value={'products':[wrong]}):
            text=sales.run({'action':'sync'})
        self.assertIn('saved Gumroad ID was not returned',text)
        self.assertIn('Connected 0',text)
        self.assertEqual(sales._product({'product':'photographer-bookings'})['url'],'')
        self.assertEqual(json.loads(self.links.read_text())['photographer-bookings']['gumroad_id'],'PHOTO-ID')

    def test_mapped_summary_uses_owned_id_and_detail_mismatch_preserves_file(self):
        self.connect_photography()
        summary={'id':'PHOTO-ID','name':'Photographer Bookings'}
        with patch.object(sales,'_cli_json',side_effect=[{'products':[summary]},
                                                       {'product':self.photography_detail()}]) as cli:
            text=sales.run({'action':'sync'})
        self.assertIn('Connected 1',text)
        self.assertEqual(cli.call_args_list[1].args[0],['products','view','PHOTO-ID'])
        before=self.links.read_bytes()
        with patch.object(sales,'_cli_json',side_effect=[{'products':[summary]},
                                                       {'product':self.photography_detail(id='WRONG-ID')}]):
            text=sales.run({'action':'sync'})
        self.assertIn('did not match',text)
        self.assertEqual(self.links.read_bytes(),before)

    def test_corrupt_link_config_is_preserved_before_direct_connection(self):
        for contents in ['corrupt configuration','[]']:
            with self.subTest(contents=contents):
                self.links.write_text(contents)
                with patch.object(sales,'_cli_json') as cli:
                    text=sales.run({'action':'link','product':'photographer-bookings',
                                    'product_url':'https://example.gumroad.com/l/photobook'})
                self.assertIn('Product links could not be read',text)
                cli.assert_not_called()
                self.assertEqual(self.links.read_text(),contents)

if __name__=='__main__':unittest.main()
