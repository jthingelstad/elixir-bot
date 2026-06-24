import asyncio
from types import SimpleNamespace

import prompts


def test_preview_job_runtime_patches_current_runtime_bot():
    import runtime.app as runtime_app
    from runtime import admin as runtime_admin

    original_bot = runtime_app.bot
    channel_config = prompts.discord_channel_configs()[0]
    channel_id = channel_config["id"]
    channel_name = channel_config["name"].lstrip("#")

    async def run_preview():
        async with runtime_admin._preview_job_runtime() as captured_posts:
            assert runtime_app.bot is not original_bot
            channel = runtime_app.bot.get_channel(channel_id)
            assert channel is not None

            text_message = await channel.send("plain preview")
            embed_message = await channel.send(embed=SimpleNamespace(title="Action Card"), view=object())

            assert text_message.id == 1
            assert embed_message.id == 2
            assert captured_posts == [
                (channel_name, "plain preview"),
                (channel_name, "[embed] Action Card"),
            ]

        assert runtime_app.bot is original_bot

    asyncio.run(run_preview())
