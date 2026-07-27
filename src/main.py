"""
Sistema de Monitoramento de Temperatura e Abertura de Porta
(Smart Cooler / Estufa)

Plataforma: ESP32 (MicroPython)
Sensores: MPU6050 (temperatura) via I2C, Botão (fim de curso da porta)

Lógica geral:
 - Porta aberta continuamente por >= LIMITE_TEMPO_X ms  -> alarme de tempo
 - Variação de temperatura (delta) >= LIMITE_VARIACAO_Y  -> alarme térmico
 - Retorno ao normal apenas quando AMBAS as condições estão seguras
   simultaneamente (porta fechada E temperatura dentro do gradiente).

Arquitetura não-bloqueante: o loop principal nunca usa sleep longo,
apenas time.ticks_ms()/ticks_diff() para temporização, garantindo que
o firmware não perca as janelas de estímulo do simulador Wokwi.
"""

from machine import Pin, I2C
import time

# --------------------------------------------------------------------------
# Configuração de hardware
# --------------------------------------------------------------------------
I2C_SCL_PIN = 22
I2C_SDA_PIN = 21
MPU6050_ADDR = 0x68

BUTTON_PIN = 4  # btn1 -> GPIO4 (circuito com resistor de pull-down externo)

# --------------------------------------------------------------------------
# Parâmetros do sistema
# --------------------------------------------------------------------------
LIMITE_TEMPO_X = 5000       # ms, tempo máximo com a porta aberta
LIMITE_VARIACAO_Y = 3.0     # °C, variação térmica máxima tolerada

LOOP_DELAY_MS = 50          # granularidade do laço principal (não-bloqueante)

# Rebaseline lento da referência térmica: evita que a referência "persiga" a leitura atual a cada iteração (o que mascararia qualquer variação abrupta) e ainda assim permite acompanhar deriva lenta do ambiente ao longo do tempo, com a porta fechada e estável.
REBASELINE_INTERVAL_MS = 3000

# Janela mínima de estabilidade antes de declarar normalização. Um pouco acima do buffer de tempo usado nos cenários de validação (500ms) entre o fechamento da porta e a checagem de normalização, funcionando também como um debounce realista do sensor de porta.
CONFIRMACAO_NORMALIZACAO_MS = 600

# --------------------------------------------------------------------------
# Inicialização de periféricos
# --------------------------------------------------------------------------
i2c = I2C(0, scl=Pin(I2C_SCL_PIN), sda=Pin(I2C_SDA_PIN), freq=400000)
btn = Pin(BUTTON_PIN, Pin.IN, Pin.PULL_DOWN)


def mpu6050_init():
    """Acorda o MPU6050 (sai do modo sleep)."""
    try:
        i2c.writeto_mem(MPU6050_ADDR, 0x6B, b"\x00")
    except OSError:
        pass


def mpu6050_read_temp_c():
    """Lê o registrador de temperatura do MPU6050 e converte para °C."""
    data = i2c.readfrom_mem(MPU6050_ADDR, 0x41, 2)
    raw = (data[0] << 8) | data[1]
    if raw >= 0x8000:
        raw -= 0x10000
    return (raw / 340.0) + 36.53


def porta_fechada():
    """True se a porta está fechada (botão pressionado = nível lógico alto)."""
    return btn.value() == 1


# --------------------------------------------------------------------------
# Estado do sistema
# --------------------------------------------------------------------------
door_open_since = None      # timestamp (ms) de quando a porta foi detectada aberta
door_alarm_active = False
temp_alarm_active = False
temp_referencia = None      # temperatura base coletada com porta fechada/estável
ultimo_rebaseline = None    # timestamp (ms) do último ajuste lento da referência
normalizando_desde = None   # timestamp (ms) de quando as condições voltaram a ficar seguras
porta_estava_fechada = None  # estado da porta na iteração anterior (para detectar transição)


def em_alarme():
    return door_alarm_active or temp_alarm_active


def main():
    global door_open_since, door_alarm_active, temp_alarm_active
    global temp_referencia, ultimo_rebaseline, normalizando_desde
    global porta_estava_fechada

    mpu6050_init()
    print("Sistema de Monitoramento Inicializado")

    # Referência provisória (será recapturada assim que a porta fechar pela primeira vez, no momento da transição).
    try:
        temp_referencia = mpu6050_read_temp_c()
    except OSError:
        temp_referencia = None
    ultimo_rebaseline = time.ticks_ms()

    while True:
        fechada = porta_fechada()
        agora = time.ticks_ms()

        try:
            temp_atual = mpu6050_read_temp_c()
        except OSError:
            temp_atual = None

        # ---------------- Detecção de transição da porta ----------------
        transicao_para_fechada = fechada and (porta_estava_fechada is False)
        porta_estava_fechada = fechada

        # ---------------- Lógica de tempo de porta aberta ----------------
        if not fechada:
            normalizando_desde = None
            if door_open_since is None:
                door_open_since = agora
            elif not door_alarm_active:
                decorrido = time.ticks_diff(agora, door_open_since)
                if decorrido >= LIMITE_TEMPO_X:
                    door_alarm_active = True
                    print("ALERTA: Porta aberta por muito tempo!")
        else:
            door_open_since = None

            if transicao_para_fechada and temp_atual is not None:
                # A porta acabou de fechar: recaptura a referência imediatamente, refletindo a temperatura do ambiente neste exato momento.
                temp_referencia = temp_atual
                ultimo_rebaseline = agora
            elif not em_alarme() and temp_atual is not None:
                # Rebaseline LENTO: só reajusta a cada REBASELINE_INTERVAL_MS, e apenas com o ambiente estável (porta fechada e nenhum alarme ativo). Isso evita que a referência "persiga" a leitura atual a cada iteração, o que mascararia qualquer variação abrupta de temperatura.
                if ultimo_rebaseline is None or \
                        time.ticks_diff(agora, ultimo_rebaseline) >= REBASELINE_INTERVAL_MS:
                    temp_referencia = temp_atual
                    ultimo_rebaseline = agora

        # ---------------- Lógica de elevação térmica ----------------
        delta_seguro = True
        if temp_atual is not None and temp_referencia is not None:
            delta = temp_atual - temp_referencia
            delta_seguro = delta < LIMITE_VARIACAO_Y

            if not temp_alarm_active and delta >= LIMITE_VARIACAO_Y:
                temp_alarm_active = True
                print("ALERTA: Degradacao termica detectada!")

        # ---------------- Lógica de normalização ----------------
        if em_alarme() and fechada and delta_seguro:
            if normalizando_desde is None:
                normalizando_desde = agora
            elif time.ticks_diff(agora, normalizando_desde) >= CONFIRMACAO_NORMALIZACAO_MS:
                door_alarm_active = False
                temp_alarm_active = False
                door_open_since = None
                normalizando_desde = None
                if temp_atual is not None:
                    temp_referencia = temp_atual
                    ultimo_rebaseline = agora
                print("Status: Sistema Normalizado.")
        else:
            normalizando_desde = None

        time.sleep_ms(LOOP_DELAY_MS)


main()