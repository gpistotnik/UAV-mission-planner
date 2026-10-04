from django.apps import AppConfig


class MissionsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "missions"
    verbose_name = "Misije"

    def ready(self) -> None:
        """Ob zagonu streznika sprozi samodejno povezavo s Pixhawkom.

        Vklop krmili ``UAV_AUTOCONNECT`` (privzeto obratno od ``DEBUG``), sam
        modul pa se dodatno umakne pri ukazih kot ``migrate`` ali ``test`` ---
        migracija ne sme odpirati serijskih vrat.
        """
        from .services.autoconnect import start_autoconnect

        try:
            start_autoconnect()
        except Exception as exc:  # pragma: no cover
            # Zagon streznika ne sme pasti zaradi povezave s krmilnikom.
            import logging
            logging.getLogger(__name__).warning(
                "Samodejna povezava s Pixhawkom ni bila zagnana: %s", exc)
