import genanki
import random
from app.config import settings


def gerar_anki(aula_id: int, titulo: str, flashcards: list[dict]) -> str:
    deck_id = 1_000_000_000 + aula_id
    model_id = 1_700_000_000 + aula_id

    model = genanki.Model(
        model_id, f"Modelo Aula {aula_id}",
        fields=[{"name": "Pergunta"}, {"name": "Resposta"}],
        templates=[{
            "name": "Cartão",
            "qfmt": "{{Pergunta}}",
            "afmt": '{{FrontSide}}<hr id="answer">{{Resposta}}',
        }],
    )

    deck = genanki.Deck(deck_id, titulo)
    for fc in flashcards:
        note = genanki.Note(
            model=model,
            fields=[fc["pergunta"], fc["resposta"]],
            guid=genanki.guid_for(f"{aula_id}-{fc['pergunta']}"),
        )
        deck.add_note(note)

    output = str(settings.anki_dir / f"aula_{aula_id}.apkg")
    genanki.Package(deck).write_to_file(output)
    return output
