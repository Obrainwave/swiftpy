import asyncio
import contextvars

ctx_var: contextvars.ContextVar[str] = contextvars.ContextVar("demo")


async def main():
    ctx_var.set("hello from the main task")
    print(f"Value inside main task: {ctx_var.get()}")
    print("Python version check passed, asyncio event loop is running")


asyncio.run(main())
