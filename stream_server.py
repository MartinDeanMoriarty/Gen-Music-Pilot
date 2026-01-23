import os
import asyncio
from aiohttp import web
from utils.player_instance import player

runner = None
site = None
loop = None

async def stream_handler(request):
    current_clip = player.get_current_clip()
    while not current_clip or not os.path.exists(current_clip):
        print(f"📡 No clip to stream! : {current_clip}")
        await asyncio.sleep(2)
        current_clip = player.get_current_clip()

    headers = {
        "Content-Type": "audio/mpeg",
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
    }

    print(f"📡 Found clip to stream: {current_clip}")
    resp = web.StreamResponse(status=200, reason="OK", headers=headers)
    await resp.prepare(request)

    try:
        with open(current_clip, "rb") as f:
            chunk = f.read(8192)
            while chunk:
                await resp.write(chunk)
                await asyncio.sleep(0.1)
                chunk = f.read(8192)
        print("📡 Sending stream!")                
    except Exception as e:
        print(f"❌ Streaming error: {e}")

    await resp.write_eof()
    return resp


async def run_stream_server():
    global runner, site

    app = web.Application()
    app.router.add_get("/live", stream_handler)

    runner = web.AppRunner(app)
    await runner.setup()

    site = web.TCPSite(runner, port=8080)
    await site.start()
    print("📡 Stream server running at http://localhost:8080/live")


def start_stream_server():
    global loop
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(run_stream_server())
    try:
        loop.run_forever()
    finally:
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()


def stop_stream_server():
    global loop, runner
    if loop and loop.is_running():
        print("🛑 Stopping stream server...")
        # Stop asyncio loop start from another thread
        asyncio.run_coroutine_threadsafe(_shutdown_server(), loop)
    else:
        print("⚠️ Server loop not running.")


async def _shutdown_server():
    global runner, loop
    print("🧹 Cleaning up server...")
    if runner:
        await runner.cleanup()
    loop.stop()
    print("✅ Stream server stopped.")
