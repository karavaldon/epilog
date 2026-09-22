from .config import Config
from .render import template_env
from .schedule import format_time, parse_time
from .send import send_email
from .state import State

SUBJECT = "⁕ Welcome to Epilog"


def send_welcome(cfg: Config, state: State) -> None:
    """Sends the welcome email; the first reply that adds accounts triggers a first digest."""
    env = template_env()
    ctx = dict(digest_time=format_time(*parse_time(cfg.digest_time)))
    html = env.get_template("welcome.html.j2").render(**ctx)
    text = env.get_template("welcome.txt.j2").render(**ctx)
    send_email(cfg, SUBJECT, text, html)
    state.welcome_pending = True
    state.save()
