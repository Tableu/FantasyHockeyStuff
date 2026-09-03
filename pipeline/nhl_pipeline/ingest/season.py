from nhl_pipeline import db


def ensure_season(cursor, season_cfg: dict) -> int:
    nhl_season_id = season_cfg["SeasonID_NHL"]
    start_year = int(str(nhl_season_id)[:4])
    end_year = int(str(nhl_season_id)[4:])
    return db.upsert_get_id(
        cursor, "Reference.Seasons", "SeasonID",
        {"NHLSeasonID": nhl_season_id},
        {"StartYear": start_year, "EndYear": end_year, "DisplayName": season_cfg["DisplayName"]},
    )
