# -*- coding: utf-8 -*-
"""Application dependency injector."""
import logging

logger = logging.getLogger(__name__)


# Database #########################################################################################
async def get_db():
    """Generates database sessions and closes them when finished."""
    from sql.database import SessionLocal  # pylint: disable=import-outside-toplevel
    logger.debug("Getting database SessionLocal")
    db = SessionLocal()
    try:
        yield db
        await db.commit()
    except:
        await db.rollback()
    finally:
        await db.close()


# asyncio.create_task(get_machine())
# asyncio.run(init_machine())
