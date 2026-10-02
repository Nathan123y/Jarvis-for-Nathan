"""Website service pitches share approval/history but never use Gumroad billing."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlsplit

from plugins import gmail, product_sales as sales


class WebsiteServiceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.payment_file = root / 'website_service.json'
        self.links_file = root / 'catalog_links.json'
        self.state_file = root / 'state.json'
        self.settings = {'gmail_account': 'spam', 'sender_name': 'Example Seller',
                         'postal_address': '123 Example Street, Example City, CA 00000'}
        self.patches = [patch.object(sales, '_WEBSITE_FILE', self.payment_file),
                        patch.object(sales, '_LINKS_FILE', self.links_file),
                        patch.object(sales, '_STATE_FILE', self.state_file),
                        patch.object(sales, 'get_plugin_config', return_value=self.settings),
                        patch.object(sales, 'get_plugin_enabled', return_value=True)]
        for item in self.patches:
            item.start()
        self.player = MagicMock()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.directory.cleanup()

    def action(self, action, **args):
        return sales.run(dict(action=action, product='soccer-coach-website', **args), player=self.player)

    def add_lead(self):
        return self.action('add_lead', email='coach@example.com', name='Example Coach',
                           relationship='Known to the user', context='Independent soccer coaching')

    def test_service_is_available_with_public_sample_before_payment_setup(self):
        with patch.object(sales, '_cli_json') as cli, patch.object(gmail, '_service') as gmail_service:
            text = self.action('brief')
            products = self.action('products')
            payment = self.action('payment')
        self.assertIn('$249 USD once', text)
        self.assertIn('one revision', text)
        self.assertIn('Domain and hosting costs are separate', text)
        self.assertIn('fictional example', text)
        self.assertIn(sales._WEBSITE_URL, text)
        self.assertIn('service; public sample connected', products)
        self.assertIn('No live Stripe payment link connected yet', payment)
        self.assertEqual(sales._product({'product': 'Soccer Coach Website'})['id'], 'soccer-coach-website')
        self.assertFalse(self.payment_file.exists())
        self.assertFalse(self.state_file.exists())
        cli.assert_not_called()
        gmail_service.assert_not_called()

    def test_default_email_is_a_service_pitch_with_demo_and_no_checkout_or_excel_claims(self):
        self.action('link', product_url='https://buy.stripe.com/exampleCheckout123')
        self.add_lead()
        with patch.object(sales, '_cli_json') as cli, patch.object(gmail, '_service') as connect:
            result = self.action('draft', email='coach@example.com')
        draft = next(iter(sales._load()['drafts'].values()))
        self.assertIn('custom one-page websites', result)
        self.assertIn('$249 USD', result)
        self.assertIn('fictional coaching website example', result)
        self.assertIn('Reply with your business name', result)
        for invalid in ['Excel', 'workbook', 'instant download', 'buy.stripe.com', 'attontios.gumroad.com']:
            self.assertNotIn(invalid, draft['body'])
        self.assertEqual(draft['product_kind'], 'service')
        self.assertEqual(draft['product_url'], sales._WEBSITE_URL)
        link = draft['body'].split(': https://', 1)[1]
        params = parse_qs(urlsplit('https://' + link).query)
        self.assertEqual(params['utm_campaign'], ['soccer-coach-website_launch'])
        self.assertEqual(params['utm_content'], [draft['id']])
        cli.assert_not_called()
        connect.assert_not_called()

    def test_campaign_is_three_service_drafts_and_posts_nothing(self):
        result = self.action('campaign', channel='facebook')
        self.assertIn('Saved 3 facebook drafts. None posted.', result)
        self.assertEqual(len(sales._load()['drafts']), 3)
        for draft in sales._load()['drafts'].values():
            self.assertIn(sales._WEBSITE_URL, draft['body'])
            self.assertIn('$249 USD', draft['body'])
            self.assertNotIn('Excel', draft['body'])

    def test_payment_link_saved_privately_without_gumroad_charge_or_other_binding_changes(self):
        sales._write_links({'tutor-sessions': {'published': True, 'url': 'https://example.gumroad.com/l/tutor'}})
        before = self.links_file.read_bytes()
        with patch.object(sales, '_cli_json') as cli, patch.object(gmail, '_send') as send:
            result = self.action('link', product_url='https://buy.stripe.com/exampleCheckout123')
        self.assertIn('Saved the Soccer Coach Website Stripe payment link', result)
        self.assertIn('not verified the checkout amount', result)
        self.assertEqual(self.links_file.read_bytes(), before)
        self.assertEqual(self.payment_file.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(self.payment_file.read_text())['payment_url'], 'https://buy.stripe.com/exampleCheckout123')
        self.assertIn('Owner-supplied Stripe payment link', self.action('payment'))
        self.assertFalse(self.state_file.exists())
        cli.assert_not_called()
        send.assert_not_called()

    def test_invalid_and_test_checkouts_leave_existing_payment_link_unchanged(self):
        self.action('link', product_url='https://buy.stripe.com/exampleCheckout123')
        before = self.payment_file.read_bytes()
        for url in ['http://buy.stripe.com/abc', 'https://buy.stripe.com/test_abc',
                    'https://buy.stripe.com/Test_abc', 'https://buy.stripe.com.evil.example/abc',
                    'https://user@buy.stripe.com/abc', 'https://buy.stripe.com:123/abc',
                    'https://buy.stripe.com/abc/other', 'https://buy.stripe.com//abc',
                    'https://buy.stripe.com/abc?price=1', 'https://buy.stripe.com/abc#test',
                    'https://buy.stripe.com/a%2Fb', 'https://buy.stripe.com/a\nb',
                    'https://example.gumroad.com/l/service']:
            with self.subTest(url=url):
                self.assertIn('Use an owner-supplied live HTTPS', self.action('link', product_url=url))
                self.assertEqual(self.payment_file.read_bytes(), before)

    def test_damaged_payment_configuration_is_preserved_without_breaking_download_brief(self):
        self.payment_file.write_text('broken payment configuration')
        self.assertIn('could not be read', self.action('payment'))
        self.assertIn('could not be read', self.action('link', product_url='https://buy.stripe.com/exampleCheckout123'))
        self.assertEqual(self.payment_file.read_text(), 'broken payment configuration')
        self.assertIn('Desktop Excel', sales.run({'action': 'brief'}))

    def test_gumroad_sync_and_sales_never_bind_or_report_website_payments(self):
        self.action('link', product_url='https://buy.stripe.com/exampleCheckout123')
        before = self.payment_file.read_bytes()
        records = [{'id': 'NOT-A-WEBSITE', 'name': 'Soccer Coach Website', 'published': True,
                    'short_url': 'https://example.gumroad.com/l/wrong'}]
        with patch.object(sales, '_cli_json', return_value={'products': records}) as cli:
            result = self.action('sync')
            self.assertEqual(cli.call_count, 1)
            cli.reset_mock()
            payments = self.action('sales')
            cli.assert_not_called()
        self.assertIn('Connected 0', result)
        self.assertNotIn('0 reported orders', payments)
        self.assertIn('Check your Stripe dashboard', payments)
        self.assertEqual(self.payment_file.read_bytes(), before)
        self.assertNotIn('soccer-coach-website', sales._read_links())
        self.assertEqual(sales._product({'product': 'soccer-coach-website'})['url'], sales._WEBSITE_URL)

    def test_existing_contact_opt_out_blocks_website_pitch(self):
        self.add_lead()
        self.action('record', email='coach@example.com', outcome='do_not_contact')
        result = self.action('draft', email='coach@example.com')
        self.assertIn('opted out', result)
        self.assertFalse(sales._load()['drafts'])

    def test_website_email_sends_only_after_confirmation_with_exact_reviewed_demo(self):
        self.add_lead()
        self.action('draft', email='coach@example.com')
        draft_id = next(iter(sales._load()['drafts']))
        captured = {}
        service = MagicMock()
        service.users.return_value.getProfile.return_value.execute.return_value = {'emailAddress': 'seller@example.com'}
        def request(**kwargs):
            captured.update(kwargs)
            return '[CONFIRMATION_PENDING]'
        with patch.object(gmail, '_service', return_value=service), \
                patch.object(sales.confirm, 'pending_title', return_value=''), \
                patch.object(sales.confirm, 'request', side_effect=request), \
                patch.object(gmail, '_send', return_value='Email sent.') as send:
            result = self.action('send', draft_id=draft_id)
            self.assertEqual(result, '[CONFIRMATION_PENDING]')
            send.assert_not_called()
            reviewed = self.player.show_content.call_args.args[1]
            self.action('link', product_url='https://buy.stripe.com/differentCheckout456')
            self.assertEqual(captured['run'](), 'Email sent.')
            send.assert_called_once()
            self.assertTrue(reviewed.endswith(send.call_args.args[3]))
            self.assertIn(sales._WEBSITE_URL, send.call_args.args[3])
            self.assertNotIn('buy.stripe.com', send.call_args.args[3])
            self.assertIn('reply STOP', send.call_args.args[3])


if __name__ == '__main__':
    unittest.main()
