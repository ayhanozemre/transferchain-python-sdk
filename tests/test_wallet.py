import os
import uuid
import unittest
from transferchain.datastructures import Config
from transferchain import wallet


class TestWalletMethods(unittest.TestCase):

    def test_create_wallet(self):
        user_id = os.environ.get('TRANSFERCHAIN_USER_ID')
        api_token = os.environ.get('TRANSFERCHAIN_API_TOKEN')
        api_secret = os.environ.get('TRANSFERCHAIN_API_SECRET')
        conf = Config(
            api_token=api_token,
            api_secret=api_secret)
        wallet_uuid = str(uuid.uuid4())
        os.environ['TRANSFERCHAIN_TEST_WALLET_UUID'] = wallet_uuid
        result = wallet.create_wallet(conf, user_id, wallet_uuid)
        self.assertEqual(True, result.success, result.error_message)

    def test_get_wallet_info(self):
        user_id = os.environ.get('TRANSFERCHAIN_USER_ID')
        api_token = os.environ.get('TRANSFERCHAIN_API_TOKEN')
        api_secret = os.environ.get('TRANSFERCHAIN_API_SECRET')
        conf = Config(
            api_token=api_token,
            api_secret=api_secret)
        wallet_uuid = os.environ['TRANSFERCHAIN_TEST_WALLET_UUID']
        result = wallet.get_wallet_info(conf, user_id, wallet_uuid)
        self.assertEqual(True, result.success, result.error_message)


if __name__ == '__main__':
    unittest.main()