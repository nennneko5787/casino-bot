import os

import dotenv

dotenv.load_dotenv()

amountName = os.environ["amount_name"]


def buildAmountText(amount: int) -> str:
    return f"{amount}{amountName}"


def buildGetAmountText(amount: int, *, md: bool = False) -> str:
    text = ""

    if amount > 0:
        if md:
            text += "+ "
        text += f"{buildAmountText(amount)}を獲得した！"
    elif amount < 0:
        if md:
            text += "- "
        text += f"{buildAmountText(abs(amount))}を失った..."
    else:
        if md:
            text += "+ "
        text += "何も貰えなかった..."

    return text
