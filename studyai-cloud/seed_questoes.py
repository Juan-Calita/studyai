"""Popular o banco de questões. Uso: python seed_questoes.py [URL_DA_API]"""
import sys
import requests

URL = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"

QUESTOES = [
    {"enunciado": "O que é fotossíntese?", "tema": "Biologia",
     "alternativas": {"A": "Processo de respiração celular", "B": "Conversão de luz em energia química por plantas", "C": "Divisão celular", "D": "Digestão"}, "gabarito": "B"},
    {"enunciado": "Qual a fórmula da área de um triângulo?", "tema": "Matemática",
     "alternativas": {"A": "base × altura", "B": "(base × altura)/2", "C": "π × r²", "D": "lado²"}, "gabarito": "B"},
    {"enunciado": "Quem escreveu Dom Casmurro?", "tema": "Literatura",
     "alternativas": {"A": "José de Alencar", "B": "Machado de Assis", "C": "Clarice Lispector", "D": "Graciliano Ramos"}, "gabarito": "B"},
    {"enunciado": "Função da clorofila nas plantas?", "tema": "Biologia",
     "alternativas": {"A": "Transporte de água", "B": "Absorção de luz para fotossíntese", "C": "Reprodução", "D": "Proteção contra pragas"}, "gabarito": "B"},
    {"enunciado": "Resolva: 2x + 4 = 10", "tema": "Matemática",
     "alternativas": {"A": "x=2", "B": "x=3", "C": "x=4", "D": "x=5"}, "gabarito": "B"},
    {"enunciado": "Capital do Brasil?", "tema": "Geografia",
     "alternativas": {"A": "Rio de Janeiro", "B": "São Paulo", "C": "Brasília", "D": "Salvador"}, "gabarito": "C"},
    {"enunciado": "O que são cloroplastos?", "tema": "Biologia",
     "alternativas": {"A": "Organelas da fotossíntese", "B": "Núcleo celular", "C": "Membrana", "D": "Ribossomos"}, "gabarito": "A"},
    {"enunciado": "Em que ano o Brasil foi descoberto?", "tema": "História",
     "alternativas": {"A": "1492", "B": "1500", "C": "1808", "D": "1822"}, "gabarito": "B"},
    {"enunciado": "Quanto é a raiz quadrada de 144?", "tema": "Matemática",
     "alternativas": {"A": "10", "B": "11", "C": "12", "D": "13"}, "gabarito": "C"},
    {"enunciado": "Principal gás produzido na fotossíntese?", "tema": "Biologia",
     "alternativas": {"A": "CO₂", "B": "O₂", "C": "N₂", "D": "H₂"}, "gabarito": "B"},
    {"enunciado": "Qual a função das mitocôndrias?", "tema": "Biologia",
     "alternativas": {"A": "Fotossíntese", "B": "Respiração celular", "C": "Síntese de proteínas", "D": "Armazenar DNA"}, "gabarito": "B"},
    {"enunciado": "O que é o ciclo de Krebs?", "tema": "Biologia",
     "alternativas": {"A": "Fase da fotossíntese", "B": "Ciclo de reações da respiração celular", "C": "Divisão celular", "D": "Replicação do DNA"}, "gabarito": "B"},
]

print(f"Importando {len(QUESTOES)} questões em {URL}...")
resp = requests.post(f"{URL}/api/questoes/importar-lote", json=QUESTOES)
print(resp.json())
print("✅ Pronto!")
