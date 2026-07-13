import os
import sqlite3
import logging
import psycopg2
from psycopg2 import OperationalError

logger = logging.getLogger(__name__)

class GridMindDBClient:
    def __init__(
        self,
        pg_host: str = "localhost",
        pg_port: int = 5432,
        pg_user: str = "postgres",
        pg_password: str = "postgres",
        pg_database: str = "gridmind",
        sqlite_path: str = "data/gridmind_storage.db"
    ):
        self.pg_config = {
            "host": pg_host,
            "port": pg_port,
            "user": pg_user,
            "password": pg_password,
            "database": pg_database,
            "connect_timeout": 2  # Fail fast
        }
        self.sqlite_path = sqlite_path
        self.mode = "postgres"
        self.connection = None
        
        # Ensure directories exist
        os.makedirs(os.path.dirname(self.sqlite_path), exist_ok=True)
        
        # Connect
        self.connect()
        self.init_schemas()

    def connect(self):
        """Attempts to connect to PostgreSQL. Falls back to SQLite if Postgres is unavailable."""
        try:
            logger.info("Attempting to connect to PostgreSQL database...")
            # Try to connect to postgres database
            self.connection = psycopg2.connect(**self.pg_config)
            self.mode = "postgres"
            logger.info("Successfully connected to PostgreSQL database.")
        except Exception as e:
            logger.warning(f"PostgreSQL connection failed ({e}). Falling back to SQLite mode.")
            try:
                self.connection = sqlite3.connect(self.sqlite_path, check_same_thread=False)
                # Enable WAL mode: allows concurrent reads while writing, prevents lock contention
                self.connection.execute("PRAGMA journal_mode = WAL;")
                # 5-second busy timeout so concurrent writers wait instead of immediately failing
                self.connection.execute("PRAGMA busy_timeout = 5000;")
                # Enable foreign keys for referential integrity
                self.connection.execute("PRAGMA foreign_keys = ON;")
                self.mode = "sqlite"
                logger.info(f"SQLite database initialized at {self.sqlite_path} (WAL mode enabled)")
            except Exception as se:
                logger.critical(f"FATAL: Both PostgreSQL and SQLite connections failed: {se}")
                raise se

    def init_schemas(self):
        """Initializes database tables for the active database engine."""
        if self.mode == "postgres":
            try:
                cursor = self.connection.cursor()
                # Read schema SQL from file
                schema_path = os.path.join(os.path.dirname(__file__), "schema.sql")
                if os.path.exists(schema_path):
                    with open(schema_path, "r") as f:
                        schema_sql = f.read()
                    
                    # Split statements because psycopg2 doesn't always handle complex multi-command blocks well
                    # especially create extension or Timescale commands that might fail
                    for statement in schema_sql.split(";"):
                        statement = statement.strip()
                        if not statement:
                            continue
                        try:
                            cursor.execute(statement)
                        except Exception as e:
                            # Rollback sub-transaction and log
                            self.connection.rollback()
                            if "timescaledb" in statement.lower():
                                logger.debug(f"TimescaleDB command skipped (likely not running standard Timescale image): {e}")
                            else:
                                logger.warning(f"SQL Statement skipped: {e}")
                    self.connection.commit()
                    logger.info("PostgreSQL schema successfully initialized.")
                else:
                    logger.warning("schema.sql file not found. Database structure must be pre-created.")
                cursor.close()
            except Exception as e:
                self.connection.rollback()
                logger.error(f"Error initializing Postgres schema: {e}")
        
        elif self.mode == "sqlite":
            try:
                cursor = self.connection.cursor()
                # Create SQLite compatible schema
                cursor.execute("""
                CREATE TABLE IF NOT EXISTS assets (
                    asset_id TEXT PRIMARY KEY,
                    asset_type TEXT NOT NULL,
                    capacity_kw REAL,
                    location TEXT
                );
                """)
                
                cursor.execute("""
                CREATE TABLE IF NOT EXISTS electricity_prices (
                    time TEXT NOT NULL,
                    buy_price_inr REAL NOT NULL,
                    sell_price_inr REAL NOT NULL,
                    grid_frequency REAL NOT NULL
                );
                """)
                
                cursor.execute("""
                CREATE TABLE IF NOT EXISTS telemetry_power (
                    time TEXT NOT NULL,
                    asset_id TEXT REFERENCES assets(asset_id),
                    power_kw REAL NOT NULL,
                    voltage REAL,
                    status TEXT NOT NULL,
                    anomaly_flag INTEGER DEFAULT 0,
                    battery_soc_pct REAL DEFAULT NULL
                );
                """)

                # Migrate existing DB: add battery_soc_pct column if not present
                try:
                    cursor.execute("ALTER TABLE telemetry_power ADD COLUMN battery_soc_pct REAL DEFAULT NULL;")
                    self.connection.commit()
                    logger.info("Migrated telemetry_power: added battery_soc_pct column.")
                except Exception:
                    pass  # Column already exists — safe to ignore

                cursor.execute("""
                CREATE TABLE IF NOT EXISTS predictions (
                    time TEXT NOT NULL,
                    target_type TEXT NOT NULL,
                    predicted_value REAL NOT NULL,
                    created_at TEXT NOT NULL
                );
                """)
                
                # Indexes for SQLite
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_sqlite_prices_time ON electricity_prices (time DESC);")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_sqlite_telemetry_asset_time ON telemetry_power (asset_id, time DESC);")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_sqlite_predictions_target_time ON predictions (target_type, time DESC);")
                
                # Unique indexes to prevent duplicate records (fixes Flaw 8)
                cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_uniq_prices ON electricity_prices (time);")
                cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_uniq_telemetry ON telemetry_power (time, asset_id);")
                cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_uniq_predictions ON predictions (time, target_type);")
                
                self.connection.commit()
                cursor.close()
                logger.info("SQLite schema successfully initialized.")
            except Exception as e:
                logger.error(f"Error initializing SQLite schema: {e}")

    def execute(self, query: str, params: tuple = None):
        """Executes a single SQL command."""
        cursor = self.connection.cursor()
        try:
            # SQLite uses '?' placeholder while PostgreSQL uses '%s'.
            # We convert '%s' placeholders to '?' automatically if we are in SQLite mode.
            if self.mode == "sqlite":
                query = query.replace("%s", "?")
            
            cursor.execute(query, params or ())
            self.connection.commit()
        except Exception as e:
            self.connection.rollback()
            raise e
        finally:
            cursor.close()

    def executemany(self, query: str, params_list: list):
        """Executes bulk SQL commands."""
        if not params_list:
            return
            
        cursor = self.connection.cursor()
        try:
            if self.mode == "sqlite":
                query = query.replace("%s", "?")
                cursor.executemany(query, params_list)
            else:
                cursor.executemany(query, params_list)
            self.connection.commit()
        except Exception as e:
            self.connection.rollback()
            raise e
        finally:
            cursor.close()

    def fetch_all(self, query: str, params: tuple = None) -> list:
        """Fetches all results from a query."""
        cursor = self.connection.cursor()
        try:
            if self.mode == "sqlite":
                query = query.replace("%s", "?")
            cursor.execute(query, params or ())
            results = cursor.fetchall()
            return results
        finally:
            cursor.close()

    def close(self):
        """Closes connection."""
        if self.connection is not None:
            self.connection.close()
            logger.info(f"Database connection ({self.mode}) closed.")
            self.connection = None

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    client = GridMindDBClient()
    client.execute("INSERT OR IGNORE INTO assets VALUES ('test', 'battery', 100.0, 'block_a');")
    res = client.fetch_all("SELECT * FROM assets;")
    print("Database assets:", res)
    client.close()
