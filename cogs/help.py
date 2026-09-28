"""ヘルプ: カテゴリごとにページを分けたボタン送り式ヘルプ。"""

import discord
from discord.ext import commands


def build_help_embeds() -> list[discord.Embed]:
    """各カテゴリのEmbedを順番に返す。Viewのセレクトと対応。"""
    pages: list[discord.Embed] = []

    intro = discord.Embed(
        title="ヘルプ📖",
        description=(
            "スラッシュ (`/help`) でもプレフィックス (`c#help`) でも使えます。\n"
            "下の ◀ ▶ ボタンかメニューでカテゴリを切り替えられます。"
        ),
        color=discord.Color.blurple(),
    )
    intro.add_field(
        name="使い方",
        value="`/help` でこの画面を開く\n各ゲームは掛け金を指定して遊ぶ",
        inline=False,
    )
    intro.add_field(
        name="カテゴリ",
        value="🎰 カジノ / ⚫ オセロ / ☖ 将棋・チェス・🤖 AI / 📈 株 / 💰 お金・借金 / 🎯 ミッション / 🆙 レベル / 🛠️ 管理者",
        inline=False,
    )
    pages.append(intro)

    casino = discord.Embed(
        title="🎰 カジノ",
        description="掛け金を払って遊ぶゲーム。勝ち分は借金があれば自動で返済に充当されます。",
        color=discord.Color.green(),
    )
    casino.add_field(
        name="/slot [賭ける額]",
        value="スロットを1回引く。もう一度ボタンで連続プレイ可",
        inline=False,
    )
    casino.add_field(
        name="/dice [掛け金] [ロールオーバー]",
        value="1〜99の目標値を決めてロール！出目が目標超えで勝ち (既定 200 / 50)",
        inline=False,
    )
    casino.add_field(
        name="/mines [掛け金] [爆弾の数]",
        value="5×4マスから安全マスを開けていく。ペイアウトで確定 (既定 100 / 3)",
        inline=False,
    )
    casino.add_field(
        name="/highlow [掛け金]",
        value="次のカードがHIGHかLOWかを当て続けて倍率を積む (既定 100)",
        inline=False,
    )
    casino.add_field(
        name="/blackjack [掛け金]",
        value="21点勝負。Hit / Standボタンで操作 (既定 100)",
        inline=False,
    )
    pages.append(casino)

    othello = discord.Embed(
        title="⚫ オセロ",
        description="CPU戦と対人戦。掛け金は開始時に徴収、勝者がペイアウトを獲得します。",
        color=discord.Color.dark_green(),
    )
    othello.add_field(
        name="/othello [掛け金] [強さ] [先手]",
        value="CPUと対戦。強さ: かんたん×1.5 / ふつう×1.9 / つよい×2.5",
        inline=False,
    )
    othello.add_field(
        name="/othello-vs [掛け金] [相手] [先手後手]",
        value="指定メンバーを指名して対人戦。勝者は掛け金×2×0.95を獲得",
        inline=False,
    )
    othello.add_field(
        name="/othello-open [掛け金] [先手後手]",
        value="対戦相手を募集。参加ボタンで対戦開始",
        inline=False,
    )
    othello.add_field(
        name="/othello-cancel",
        value="自分の募集中ロビーを取り消す (詰まった時用)",
        inline=False,
    )
    pages.append(othello)

    board2 = discord.Embed(
        title="☖ 将棋・♔ チェス",
        description="駒選択→移動先選択の2段階操作。移動先選択中はキャンセル可。掛け金は開始時に徴収、勝者がペイアウトを獲得します。",
        color=discord.Color.dark_gold(),
    )
    board2.add_field(
        name="/shogi [掛け金] [強さ] [先手] / /chess [掛け金] [強さ] [先手]",
        value="CPUと対戦。強さ: かんたん×1.5 / ふつう×1.9 / つよい×2.5",
        inline=False,
    )
    board2.add_field(
        name="/shogi-vs・/chess-vs [掛け金] [相手] [先手後手]",
        value="指定メンバーを指名して対人戦。勝者は掛け金×2×0.95を獲得",
        inline=False,
    )
    board2.add_field(
        name="/shogi-open・/chess-open [掛け金] [先手後手]",
        value="対戦相手を募集。参加ボタンで対戦開始",
        inline=False,
    )
    board2.add_field(
        name="/ai chat・price・clear・persona",
        value="AIチャット (料金は通貨指数連動・/ai priceで確認)。メンション/リプライでも呼出可。履歴は/ai clearで削除",
        inline=False,
    )
    pages.append(board2)

    stock = discord.Embed(
        title="📈 株",
        description="株価は1分ごとに自律変動。売買の需給影響は1回±3%までに抑えられます。",
        color=discord.Color.blue(),
    )
    stock.add_field(
        name="/stock list",
        value="上場中の銘柄一覧と直近の値動きを表示",
        inline=False,
    )
    stock.add_field(
        name="/stock buy [銘柄] [数量] / /stock sell [銘柄] [数量]",
        value="株を売買する。取扱停止中は買えない (売りは可)。"
        "自社株は売却のみ可。他人の会社の売買では代金の1%が設立者に還元される",
        inline=False,
    )
    stock.add_field(
        name="/stock create [銘柄] [投資額]",
        value="会社を設立 (1人1社)。投資額でランク・条件が決まり、創業者株 (売却のみ可) が付く。"
        "他人の売買でロイヤリティ1%、値上がりで保有株数連動の配当が入る",
        inline=False,
    )
    stock.add_field(
        name="/stock chart [銘柄] [件数]",
        value="株価の折れ線チャートを表示 (最大200件)",
        inline=False,
    )
    stock.add_field(
        name="/stock portfolio",
        value="保有株・平均取得単価・評価額・損益を表示",
        inline=False,
    )
    pages.append(stock)

    money = discord.Embed(
        title="💰 お金・借金・番付",
        description="残高・送金・借金・番付の確認ができます。",
        color=discord.Color.gold(),
    )
    money.add_field(
        name="/stats [対象]",
        value="残高・株評価額・借金・総資産を表示 (省略時は自分)",
        inline=False,
    )
    money.add_field(
        name="/send [あげる額] [対象]",
        value="他のメンバーに所持金を譲渡する",
        inline=False,
    )
    money.add_field(
        name="/ranking / /poor-ranking",
        value="総資産 (残高 + 株評価額 − 借金) の上位 / 下位を表示",
        inline=False,
    )
    money.add_field(
        name="/loan borrow [借りる額] / repay [返済額] / status",
        value="借金する・返す・状況を見る。入金は自動で返済に充当 (手数料10%)",
        inline=False,
    )
    pages.append(money)

    mission = discord.Embed(
        title="🎯 ミッション",
        description="発言・VC・ゲーム・株取引で通貨を稼げます。期限内に受け取らないと失効します (恒常は除く)。",
        color=discord.Color.purple(),
    )
    mission.add_field(
        name="/mission",
        value="アワー/デイリー/ウィークリー/マンスリー/恒常の進捗確認・🎁ボタンで受取",
        inline=False,
    )
    mission.add_field(
        name="発言・VC",
        value="チャット送信やVC滞在で進行。指定ch系は管理者設定のchのみ",
        inline=False,
    )
    mission.add_field(
        name="ゲーム・株",
        value="カジノ・オセロ・株売買で進行。報酬は借金があれば自動返済に充当",
        inline=False,
    )
    mission.add_field(
        name="レベル系",
        value="XP獲得 (H4/D6/W5/M4/P6/P7)・レベルアップ (D7/W6/M5/P8/P9) でも進行",
        inline=False,
    )
    mission.add_field(
        name="❓隠しミッション",
        value="達成するまで一覧に表示されません。探してみよう！",
        inline=False,
    )
    pages.append(mission)

    level = discord.Embed(
        title="🆙 レベル",
        description=(
            "チャット・VC滞在でXPを稼ぎ、レベルアップで通貨報酬がもらえます "
            "(報酬は借金があれば自動返済に充当)。"
        ),
        color=discord.Color.blurple(),
    )
    level.add_field(
        name="/level [対象]",
        value="レベル・XP・進捗・発言数・VC時間・順位を表示 (省略時は自分)",
        inline=False,
    )
    level.add_field(
        name="/level-ranking",
        value="XP上位10名のレベル番付を表示",
        inline=False,
    )
    level.add_field(
        name="XPの稼ぎ方",
        value=(
            "チャット1通: 15〜25+長文ボーナス (60秒CD、短文・連投は半減)\n"
            "VC1分: 8〜12 (5分ごとに自動加算)\n"
            "必要XP: 5*Lv^2+50*Lv+100 / 報酬: 到達Lv×50"
        ),
        inline=False,
    )
    pages.append(level)

    admin = discord.Embed(
        title="🛠️ 管理者専用",
        description="サーバー管理権限が必要です。",
        color=discord.Color.red(),
    )
    admin.add_field(
        name="/give [あげる額] [対象]",
        value="無から所持金を生成して付与する",
        inline=False,
    )
    admin.add_field(
        name="/stock-admin add [銘柄] [開始価格]",
        value="銘柄を上場。mu / sigma / impact は数値かプリセットで指定可",
        inline=False,
    )
    admin.add_field(
        name="/stock-admin delist / relist [銘柄]",
        value="取扱停止 / 再開 (停止中も売却は可能)",
        inline=False,
    )
    admin.add_field(
        name="/stock-admin params [銘柄]",
        value="mu / sigma / impact を変更する",
        inline=False,
    )
    admin.add_field(
        name="/mission-admin channel [ch] / unset-channel / status",
        value="ミッションの指定chを設定・解除・確認する",
        inline=False,
    )
    admin.add_field(
        name="/level-admin add [対象] [XP量] / reset [対象]",
        value="XPを付与する・レベル情報を初期化する",
        inline=False,
    )
    admin.add_field(
        name="c#sync ※プレフィックスのみ",
        value="スラッシュコマンドを手動同期する (起動時は自動同期しない)",
        inline=False,
    )
    pages.append(admin)

    for i, embed in enumerate(pages):
        embed.set_footer(text=f"{i + 1}/{len(pages)}ページ ◀ ▶かメニューで移動")
    return pages


class HelpPageSelect(discord.ui.Select):
    """カテゴリジャンプ用セレクト。"""

    def __init__(self, embeds: list[discord.Embed]):
        options = [
            discord.SelectOption(label=e.title or f"{i + 1}ページ", value=str(i))
            for i, e in enumerate(embeds)
        ]
        super().__init__(placeholder="カテゴリを選択...", options=options, row=0)

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        assert isinstance(view, HelpView)
        if not await view.check_user(interaction):
            return
        await interaction.response.defer()
        if self.values:
            view.page = int(self.values[0])
        await view.render(interaction)


class HelpView(discord.ui.View):
    """ヘルプのページ送りView。操作はコマンド実行者のみ。"""

    def __init__(self, author_id: int, embeds: list[discord.Embed]):
        super().__init__(timeout=180)
        self.author_id = author_id
        self.embeds = embeds
        self.page = 0
        self.page_select = HelpPageSelect(embeds)
        self.add_item(self.page_select)

    async def check_user(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "コマンド実行者のみ操作できます", ephemeral=True
            )
            return False
        return True

    async def render(self, interaction: discord.Interaction):
        self.page_select.placeholder = self.embeds[self.page].title
        for option, i in zip(self.page_select.options, range(len(self.embeds))):
            option.default = i == self.page
        message = interaction.message
        if message is not None:
            await message.edit(embed=self.embeds[self.page], view=self)
        else:
            await interaction.response.edit_message(
                embed=self.embeds[self.page], view=self
            )

    @discord.ui.button(label="◀", style=discord.ButtonStyle.primary, row=1)
    async def prev(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.check_user(interaction):
            return
        await interaction.response.defer()
        self.page = (self.page - 1) % len(self.embeds)
        await self.render(interaction)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.primary, row=1)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.check_user(interaction):
            return
        await interaction.response.defer()
        self.page = (self.page + 1) % len(self.embeds)
        await self.render(interaction)

    async def on_timeout(self):
        for item in self.children:
            if isinstance(item, (discord.ui.Button, discord.ui.Select)):
                item.disabled = True


class HelpCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.hybrid_command("help", brief="ヘルプを表示します")
    @commands.guild_only()
    async def helpCommand(self, ctx: commands.Context):
        embeds = build_help_embeds()
        await ctx.reply(embed=embeds[0], view=HelpView(ctx.author.id, embeds))


async def setup(bot: commands.Bot):
    await bot.add_cog(HelpCog(bot))
