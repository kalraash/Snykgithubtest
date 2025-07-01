  @app.get("/shutdown")
    async def _shutdown(request: Request) -> JSONResponse:
        """Kill backend server from inside."""
        logger.info("Stopping services on demand...")
        pause_all_services()
        logger.info("Stopping services on demand done.")
        app._server.should_exit = True  # pylint: disable=protected-access
        await asyncio.sleep(0.3)
        return {"stopped": True}
