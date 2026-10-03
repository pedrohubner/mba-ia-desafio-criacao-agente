from . import condominio, config


def main() -> None:
    for sufixo in ("", "-wal", "-shm"):
        config.SESSOES_DB.with_name(config.SESSOES_DB.name + sufixo).unlink(missing_ok=True)
    condominio.restaurar()
    print("Dados restaurados a partir de dados/ e sessões apagadas.")


if __name__ == "__main__":
    main()
