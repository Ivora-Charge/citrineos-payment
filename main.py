from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from api.api import api_router
from asyncio import get_event_loop
from config import Config
from fastapi import APIRouter, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from logging import basicConfig
from integrations.directus.directus import DirectusIntegration
from integrations.citrineos.citrineos import CitrineOSIntegration
from uvicorn import run
import stripe

from db.init_db import init_db, SessionLocal
from catalog.sync import upsert_payment_catalog
from integrations.integration import FileIntegration, OcppIntegration
from logging import error, info

basicConfig(format=Config.LOG_FORMAT, level=Config.LOG_LEVEL)

""" Create the Fast API web app and define cors. API router used to attach root path from Config. """
app = FastAPI(
    title=Config.OPENAPI_TITLE,
)
router = APIRouter()

origins = [
    "*",
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

""" On startup of the web app also start the event consumer and set stripe api key """
stripe.api_key = Config.STRIPE_API_KEY

file_integration: FileIntegration = DirectusIntegration(
    Config.CITRINEOS_DIRECTUS_URL,
    Config.CITRINEOS_DIRECTUS_LOGIN_EMAIL,
    Config.CITRINEOS_DIRECTUS_LOGIN_PASSWORD,
)
ocpp_integration: OcppIntegration = CitrineOSIntegration(file_integration)
app.ocpp_integration = ocpp_integration


def _auto_seed() -> None:
    """Dev-only bootstrap: seed one catalog chain from Config.SEED_* when
    AUTO_SEED=true. Delegates to the same upsert_payment_catalog used by seed.py
    and the /catalog/sync API, so it is idempotent and safe to re-run on boot."""
    if not Config.AUTO_SEED:
        return
    db = SessionLocal()
    try:
        result = upsert_payment_catalog(
            db,
            operator_name=Config.SEED_OPERATOR_NAME,
            stripe_account_id=Config.SEED_STRIPE_ACCOUNT_ID,
            location_id=Config.SEED_LOCATION_ID,
            address="1 Test St",
            postal_code="00000",
            city="Testville",
            state="TS",
            country="USA",
            station_id=Config.SEED_STATION_ID,
            tenant_id=Config.SEED_TENANT_ID,
            ocpp_evse_id=Config.SEED_OCPP_EVSE_ID,
            evse_id=Config.SEED_EVSE_ID,
            currency=Config.SEED_CURRENCY,
            authorization_amount=Config.SEED_AUTHORIZATION_AMOUNT,
        )
        db.commit()
        info(
            f" [AUTO_SEED] Seeded station_id={Config.SEED_STATION_ID} "
            f"tenant_id={Config.SEED_TENANT_ID} (evse row id={result['evse_id']})."
        )
    except Exception as exc:  # noqa: BLE001 - never let a seed failure block boot
        db.rollback()
        error(f" [AUTO_SEED] failed: {exc}")
    finally:
        db.close()


@app.on_event("startup")
async def startup_event():
    _auto_seed()
    loop = get_event_loop()
    # Hold a strong reference to the consumer task. asyncio only keeps a weak
    # reference to tasks, so a fire-and-forget create_task() can be garbage
    # collected mid-flight ("Task was destroyed but it is pending!"), silently
    # killing event consumption. Keeping it on app.state prevents that.
    app.state.event_consumer_task = loop.create_task(
        coro=ocpp_integration.receive_events()
    )


""" Add the API router to the web app """
app.include_router(
    api_router,
    prefix=Config.WEBSERVER_PATH,
)


""" Add a health check route """


@app.get("/health_check")
async def health_check():
    return {"status": "healthy"}


""" Add the frontend web app """
templates = Jinja2Templates(directory="frontend/build")
frontend_routes = ["/", "/checkout/{evse_id}", "/charging/{evse_id}/{checkout_id}"]


async def serve_frontend(
    request: Request,
):
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "CLIENT_API_URL": Config.CLIENT_URL + "/api",
        },
    )


for route in frontend_routes:
    app.get(route, response_class=HTMLResponse)(serve_frontend)
app.mount(
    "/",
    StaticFiles(
        directory="frontend/build",
    ),
    name="frontend",
)


# Check if DB is reachable
# db = get_db()
# db.execute(text("select (1)"))
init_db()

if __name__ == "__main__":
    try:
        run(app, host=Config.WEBSERVER_HOST, port=Config.WEBSERVER_PORT)
    except Exception as e:
        # logger.error(e)
        raise e
