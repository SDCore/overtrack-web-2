import json
import logging
from datetime import datetime
from typing import Optional
from urllib.parse import urlparse

import boto3
import requests
from flask import Flask, Response, render_template, url_for as flask_url_for
from flask_bootstrap import Bootstrap

from api.authentication import check_authentication, require_authentication
from api.session import session
from blueprints.discord_bot import discord_bot_blueprint
from blueprints.login import login, require_login
from blueprints.results import results_blueprint
from models.apex_game_summary import ApexGameSummary
from overtrack.util import s2ts

app = Flask(__name__)
bootstrap = Bootstrap(app)

logs = boto3.client('logs')
s3 = boto3.client('s3')

logger = logging.getLogger(__name__)

app.register_blueprint(login)
app.register_blueprint(discord_bot_blueprint, url_prefix='/discord_bot')
app.register_blueprint(results_blueprint, url_prefix='/results')


def url_for(endpoint, **values):
    if endpoint == 'static'\
            and 'filename' in values \
            and values['filename'] not in ['style.css', 'map.js'] \
            and not values['filename'].endswith('.js'):
        return 'https://d2igtsro72if25.cloudfront.net/1/' + values['filename']
    else:
        return flask_url_for(endpoint, **values)


app.jinja_env.globals['url_for'] = url_for


@app.template_filter()
def ifnone(v, o):
    if v is None:
        print(v, '>', o)
        return o
    else:
        return v


def to_ordinal(n: int) -> str:
    suffixes = {1: 'st', 2: 'nd', 3: 'rd'}
    i = n if n < 20 else n % 10
    return f'{n}{suffixes.get(i, "th")}'


def image_url(champ: Optional[str], large: bool = False) -> str:
    if large:
        return url_for('static', filename=f'images/{champ.lower()}_large.png' if champ else '')
    else:
        return url_for('static', filename=f'images/{champ.lower()}.png' if champ else 'images/unknown.png')


COLOURS = {
    'octane': '#486F3B',
    'mirage': '#D09B49',
    'bloodhound': '#AD2A33',
    'gibraltar': '#6B4B3C',
    'caustic': '#689122',
    'pathfinder': '#58859F',
    'wraith': '#5F439F',
    'bangalore': '#572C23',
    'lifeline': '#C243D8'
}
def champion_colour(champ: str) -> str:
    return COLOURS.get(champ.lower() if champ else None)


def strftime(t: float):
    dt = datetime.fromtimestamp(t)
    date = dt.strftime('%c').split(':', 1)[0].rsplit(' ', 1)[0]
    return date + ' ' + dt.strftime('%I:%M %p')


def duration(t: Optional[float]):
    if t is None:
        return '?'
    return s2ts(t).split(':', 1)[1]


@app.route('/client')
def client():
    return render_template('client.html')


base_context = {
    'to_ordinal': to_ordinal,
    's2ts': duration,
    'strftime': strftime,
    'image_url': image_url,
    'champion_colour': champion_colour,
}


@app.route('/welcome')
def welcome():
    return render_template('welcome.html')


@app.route('/')
def root():
    if check_authentication() is None:
        games = list(ApexGameSummary.user_id_time_index.query(session.user_id, newest_first=True))
        if not len(games):
            return render_template('client.html', no_games_alert=True)
        return render_template(
            'games/games.html',
            games=games,
            **base_context
        )
    else:
        return render_template('welcome.html')


@app.route('/games')
@require_login
def games_list():
    games = list(ApexGameSummary.user_id_time_index.query(session.user_id, newest_first=True))
    if not len(games):
        return render_template('client.html', no_games_alert=True)
    return render_template(
        'games/games.html',
        games=games,
        **base_context
    )


@app.route('/game/<path:key>')
def game(key: str):
    summary = ApexGameSummary.get(key)
    logger.info(f'Fetching {summary.url}')

    url = urlparse(summary.url)
    game_object = s3.get_object(
        Bucket=url.netloc.split('.')[0],
        Key=url.path[1:]
    )
    game_data = json.loads(game_object['Body'].read())

    # used for link previews
    og_title = f'{game_data["squad"]["player"]["name"]} placed #{summary.placed}'  # TODO: find another way of getting the name
    theme_color = {
        1: '#ffdf00',
        2: '#ef20ff',
        3: '#d95ff'
    }.get(summary.placed, '#992e26')
    og_description = f'{summary.kills} Kills'
    if summary.knockdowns:
        og_description += f'\n{summary.knockdowns} Knockdowns'
    if summary.squad_kills:
        og_description += f'\n{summary.squad_kills} Squad Kills'
    if summary.landed != 'Unknown':
        og_description += f'\nDropped {summary.landed}'

    if check_authentication() is None and session.superuser:
        if 'frames' in game_object['Metadata']:
            frames_url = urlparse(game_object['Metadata']['frames'])
            frames_object = s3.get_object(
                Bucket=frames_url.netloc,
                Key=frames_url.path[1:]
            )
            frames_metadata = frames_object['Metadata']
            if 'log' in frames_metadata:
                del frames_metadata['log']  # already have this
            frames_metadata['_href'] = s3.generate_presigned_url(
                'get_object',
                Params={
                    'Bucket': frames_url.netloc,
                    'Key': frames_url.path[1:]
                }
            )

        else:
            frames_metadata = None

        if 'log' in game_object['Metadata'] and 'start' in game_object['Metadata']['log']:
            log_url = urlparse(game_object['Metadata']['log'])
            print(log_url.fragment)
            log_params = dict(e.split('=') for e in log_url.fragment.split(':', 1)[1].split(';'))
            print(log_params)

            log_time = datetime.strptime(log_params['start'], "%Y-%m-%dT%H:%M:%SZ")
            tz_offset = datetime.now() - datetime.utcnow()
            log_data = logs.get_log_events(
                logGroupName=log_params['group'],
                logStreamName=log_params['stream'],
                startTime=int((log_time + tz_offset).timestamp() * 1000)
            )
            log_lines = []
            for i, e in enumerate(log_data['events']):
                log_lines.append(e['message'].strip())
                if i > 10 and 'END RequestId' in e['message']:
                    break
        else:
            log_lines = []

        game_metadata = game_object['Metadata']
        game_metadata['_href'] = summary.url

        admin_data = {
            'game_metadata': game_metadata,
            'frames_metadata': frames_metadata,
            'log': log_lines
        }
    else:
        admin_data = None

    return render_template(
        'game/game.html',
        summary=summary,
        game=game_data,

        og_title=og_title,
        theme_colour=theme_color,
        og_description=og_description,

        admin_data=admin_data,

        **base_context
    )


# TODO: support a summary in link previews for games lists


@app.route('/eeveea_')
def eeveea_games():
    return render_template(
        'games/games.html',
        games=ApexGameSummary.user_id_time_index.query(347766573, newest_first=True),
        **base_context
    )


@app.route('/mendokusaii')
def mendokusaii_games():
    return render_template(
        'games/games.html',
        games=ApexGameSummary.user_id_time_index.query(-3, newest_first=True),
        **base_context
    )


@app.route("/by_key/<string:key>")
@require_authentication(superuser_required=True)
def games_by_key(key: str):
    return render_template(
        'games/games.html',
        games=ApexGameSummary.user_id_time_index.query(int(key), newest_first=True),
        **base_context
    )


from overtrack.util.logging_config import config_logger
config_logger(__name__, logging.INFO, False)
