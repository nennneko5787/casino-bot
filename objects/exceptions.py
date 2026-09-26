import os

import dotenv

dotenv.load_dotenv()

amountName = os.environ["amount_name"]


class CasinoBaseException(Exception):
    code: str
    message: str

    def __init__(self):
        self.code = "BASE_ERROR"
        self.message = "これはテストです。"


class AccountCreationFailed(CasinoBaseException):
    def __init__(self):
        super().__init__()
        self.code = "ACCOUNT_CREATION_FAILED"
        self.message = "カジノアカウントの登録に失敗しました。"


class AmountNotEnough(CasinoBaseException):
    def __init__(self):
        super().__init__()
        self.code = "AMOUNT_NOT_ENOUGH"
        self.message = f"{amountName} が足りません。"


class EmojiNotFound(CasinoBaseException):
    def __init__(self):
        super().__init__()
        self.code = "EMOJI_NOT_FOUND"
        self.message = "絵文字がありません。"


class YouMustDie(CasinoBaseException):
    def __init__(self):
        super().__init__()
        self.code = "YOU_MUST_DIE"
        self.message = "※対策済みです"
