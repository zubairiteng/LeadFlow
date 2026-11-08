from .common import bp


def register(app):
    from . import core, rounds, settings, public  # noqa: F401  (routes register on import)
    app.register_blueprint(bp)
    app.csrf_exempt.update({"main.unsubscribe", "main.subscribe_post", "main.health"})
    from .common import register_errors
    register_errors(app)
