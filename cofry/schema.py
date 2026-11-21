from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator

Seconds = Annotated[float, Field(ge=0, le=86400, allow_inf_nan=False)]
Style = Literal["branco", "amarelo", "neon_verde", "caixa", "minimalista",
                "editorial", "karaoke", "contorno", "italico_caixa", "laranja"]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Options(Model):
    quantidade: int = Field(3, ge=1, le=20)
    duracao_min: Seconds = 15
    duracao_pref: Seconds = 35
    duracao_max: Seconds = 60
    formato: Literal["9:16", "1:1", "16:9"] = "9:16"
    enquadramento: Literal["preencher", "inteiro", "desfocado"] = "preencher"
    seguir_rosto: bool = False
    estilo_legenda: Style = "branco"
    posicao_legenda: Literal["superior", "central", "inferior"] = "inferior"
    gancho: bool = True
    manchete: bool = False
    idioma: str = Field("pt", pattern=r"^(auto|[a-z]{2,3}(?:-[A-Za-z]{2,4})?)$")
    foco: str = Field("", max_length=1000)
    palavras_chave: list[Annotated[str, Field(max_length=100)]] = Field(default_factory=list, max_length=30)
    evitar: list[Annotated[str, Field(max_length=100)]] = Field(default_factory=list, max_length=30)
    trecho_inicio: Seconds = 0
    trecho_fim: Seconds | None = None

    @model_validator(mode="after")
    def check_times(self):
        if not 1 <= self.duracao_min <= self.duracao_pref <= self.duracao_max <= 300:
            raise ValueError("Informe durações ordenadas entre 1 e 300 segundos.")
        if self.trecho_fim is not None and self.trecho_fim <= self.trecho_inicio:
            raise ValueError("O fim do trecho deve vir depois do início.")
        return self


class Job(Model):
    job_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,100}$")
    video_url: str = Field(max_length=12000)
    options: Options = Field(default_factory=Options)
    callback_url: str = Field(max_length=12000)


class Block(Model):
    inicio: Seconds
    fim: Seconds
    texto: str = Field(min_length=1, max_length=400)

    @model_validator(mode="after")
    def check_times(self):
        if self.fim <= self.inicio:
            raise ValueError("O fim da legenda deve vir depois do início.")
        return self


class Captions(Model):
    blocos: list[Block] = Field(max_length=1000)
    estilo: Style = "branco"


class Edit(Model):
    inicio: Seconds
    fim: Seconds
    legenda: Captions | None = None
    estilo_legenda: Style | None = None
    gancho: bool | Annotated[str, Field(min_length=1, max_length=120)] | None = None
    manchete: bool | Annotated[str, Field(min_length=1, max_length=160)] | None = None

    @model_validator(mode="after")
    def check_times(self):
        if not 1 <= self.fim - self.inicio <= 300:
            raise ValueError("O corte deve durar de 1 a 300 segundos.")
        return self


class Social(Model):
    instagram: str = Field(min_length=5, max_length=2200)
    tiktok: str = Field(min_length=5, max_length=2200)
    youtube: str = Field(min_length=5, max_length=2200)

    @model_validator(mode="after")
    def check_captions(self):
        values = [getattr(self, key) for key in type(self).model_fields]
        if len({text.casefold() for text in values}) != 3 or any("#" not in text for text in values):
            raise ValueError("Gere três legendas distintas com hashtags.")
        return self


class Candidate(Model):
    primeira: int = Field(ge=0)
    ultima: int = Field(ge=0)
    titulo: str = Field(min_length=3, max_length=100)
    gancho: str = Field(min_length=3, max_length=72)
    nota: int = Field(ge=0, le=100)
    motivo: str = Field(min_length=3, max_length=400)
    legendas: Social


class Selection(Model):
    cortes: list[Candidate] = Field(max_length=30)
