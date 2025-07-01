class OperateApp:
    """Operate app."""

    def __init__(
        self,
        home: t.Optional[Path] = None,
        logger: t.Optional[logging.Logger] = None,
    ) -> None:
        """Initialize object."""
        super().__init__()
        self._path = (home or OPERATE_HOME).resolve()
        self._services = self._path / SERVICES
        self._keys = self._path / KEYS
        self._master_key = self._path / KEY
        self.setup()

        self.logger = logger or setup_logger(name="operate")
        self.keys_manager = services.manage.KeysManager(
            path=self._keys,
            logger=self.logger,
        )
        self.password: t.Optional[str] = os.environ.get("OPERATE_USER_PASSWORD")

        mm = MigrationManager(self._path, self.logger)
        mm.migrate_user_account()

    def create_user_account(self, password: str) -> UserAccount:
        """Create a user account."""
        self.password = password
        return UserAccount.new(
            password=password,
            path=self._path / "user.json",
        )

    def update_password(self, old_password: str, new_password: str) -> None:
        """Updates current password"""

        if not new_password:
            raise ValueError("You must provide a new password.")

        if not (
            self.user_account.is_valid(old_password)
            and self.wallet_manager.is_password_valid(old_password)
        ):
            raise ValueError("Password is not valid.")

        wallet_manager = self.wallet_manager
        wallet_manager.password = old_password
        wallet_manager.update_password(new_password)
        self.user_account.update(old_password, new_password)

    def update_password_with_mnemonic(self, mnemonic: str, new_password: str) -> None:
        """Updates current password using the mnemonic"""

        if not new_password:
            raise ValueError("You must provide a new password.")

        mnemonic = mnemonic.strip().lower()
        if not self.wallet_manager.is_mnemonic_valid(mnemonic):
            raise ValueError("Seed phrase is not valid.")

        wallet_manager = self.wallet_manager
        wallet_manager.update_password_with_mnemonic(mnemonic, new_password)
        self.user_account.force_update(new_password)

    def service_manager(
        self, skip_dependency_check: t.Optional[bool] = False
    ) -> services.manage.ServiceManager:
        """Load service manager."""
        return services.manage.ServiceManager(
            path=self._services,
            keys_manager=self.keys_manager,
            wallet_manager=self.wallet_manager,
            logger=self.logger,
            skip_dependency_check=skip_dependency_check,
        )

    @property
    def user_account(self) -> t.Optional[UserAccount]:
        """Load user account."""
        return (
            UserAccount.load(self._path / "user.json")
            if (self._path / "user.json").exists()
            else None
        )

    @property
    def wallet_manager(self) -> MasterWalletManager:
        """Load master wallet."""
        manager = MasterWalletManager(
            path=self._path / "wallets",
            password=self.password,
        )
        manager.setup()
        return manager

    @property
    def bridge_manager(self) -> BridgeManager:
        """Load master wallet."""
        manager = BridgeManager(
            path=self._path / "bridge",
            wallet_manager=self.wallet_manager,
        )
        return manager

    def setup(self) -> None:
        """Make the root directory."""
        self._path.mkdir(exist_ok=True)
        self._services.mkdir(exist_ok=True)
        self._keys.mkdir(exist_ok=True)

    @property
    def json(self) -> dict:
        """Json representation of the app."""
        return {
            "name": "Operate HTTP server",
            "version": "0.1.0.rc0",
            "home": str(self._path),
        }


def create_app(  # pylint: disable=too-many-locals, unused-argument, too-many-statements
    home: t.Optional[Path] = None,
) -> FastAPI:
    """Create FastAPI object."""
    HEALTH_CHECKER_OFF = os.environ.get("HEALTH_CHECKER_OFF", "0") == "1"
    number_of_fails = int(
        os.environ.get(
            "HEALTH_CHECKER_TRIES", str(HealthChecker.NUMBER_OF_FAILS_DEFAULT)
        )
    )

    logger = setup_logger(name="operate")
    if HEALTH_CHECKER_OFF:
        logger.warning("Healthchecker is off!!!")
    operate = OperateApp(home=home, logger=logger)

    operate.service_manager().log_directories()
    logger.info("Migrating service configs...")
    operate.service_manager().migrate_service_configs()
    logger.info("Migrating service configs done.")
    operate.service_manager().log_directories()

    logger.info("Migrating wallet configs...")
    operate.wallet_manager.migrate_wallet_configs()
    logger.info("Migrating wallet configs done.")

    funding_jobs: t.Dict[str, asyncio.Task] = {}
    health_checker = HealthChecker(
        operate.service_manager(), number_of_fails=number_of_fails
    )
    # Create shutdown endpoint
    shutdown_endpoint = uuid.uuid4().hex
    (operate._path / "operate.kill").write_text(  # pylint: disable=protected-access
        shutdown_endpoint
    )
    thread_pool_executor = ThreadPoolExecutor()

    async def run_in_executor(fn: t.Callable, *args: t.Any) -> t.Any:
        loop = asyncio.get_event_loop()
        future = loop.run_in_executor(thread_pool_executor, fn, *args)
        res = await future
        exception = future.exception()
        if exception is not None:
            raise exception
        return res

    def schedule_funding_job(
        service_config_id: str,
        from_safe: bool = True,
    ) -> None:
        """Schedule a funding job."""
        logger.info(f"Starting funding job for {service_config_id}")
        if service_config_id in funding_jobs:
            logger.info(f"Cancelling existing funding job for {service_config_id}")
            cancel_funding_job(service_config_id=service_config_id)

        loop = asyncio.get_running_loop()
        funding_jobs[service_config_id] = loop.create_task(
            operate.service_manager().funding_job(
                service_config_id=service_config_id,
                loop=loop,
                from_safe=from_safe,
            )
        )

    def schedule_healthcheck_job(
        service_config_id: str,
    ) -> None:
        """Schedule a healthcheck job."""
        if not HEALTH_CHECKER_OFF:
            # dont start health checker if it's switched off
            health_checker.start_for_service(service_config_id)

    def cancel_funding_job(service_config_id: str) -> None:
        """Cancel funding job."""
        if service_config_id not in funding_jobs:
            return
        status = funding_jobs[service_config_id].cancel()
        if not status:
            logger.info(f"Funding job cancellation for {service_config_id} failed")

    def pause_all_services_on_startup() -> None:
        logger.info("Stopping services on startup...")
        pause_all_services()
        logger.info("Stopping services on startup done.")

    def pause_all_services() -> None:
        service_config_ids = [
            i["service_config_id"] for i in operate.service_manager().json
        ]

        for service_config_id in service_config_ids:
            logger.info(f"Stopping service {service_config_id=}")
            if not operate.service_manager().exists(
                service_config_id=service_config_id
            ):
                continue
            deployment = (
                operate.service_manager()
                .load(service_config_id=service_config_id)
                .deployment
            )
            if deployment.status == DeploymentStatus.DELETED:
                continue
            logger.info(f"stopping service {service_config_id}")
            deployment.stop(force=True)
            logger.info(f"Cancelling funding job for {service_config_id}")
            cancel_funding_job(service_config_id=service_config_id)
            health_checker.stop_for_service(service_config_id=service_config_id)

    def pause_all_services_on_exit(signum: int, frame: t.Optional[FrameType]) -> None:
        logger.info("Stopping services on exit...")
        pause_all_services()
        logger.info("Stopping services on exit done.")

    signal.signal(signal.SIGINT, pause_all_services_on_exit)
    signal.signal(signal.SIGTERM, pause_all_services_on_exit)

    # on backend app started we assume there are now started agents, so we force to pause all
    pause_all_services_on_startup()

    # stop all services at  middleware exit
    atexit.register(pause_all_services)

    app = FastAPI()

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    )
