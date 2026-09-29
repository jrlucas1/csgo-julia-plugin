/**
 * jev_bot.sp — o "corpo" do bot.
 *
 * Assume o controle de UM bot, manda o estado dele por UDP para o bridge
 * (o "cérebro") e executa a ação tática que volta. Mira e tiro ficam aqui,
 * no plugin: o modelo só escolhe O QUE fazer nos próximos ~0.35 s.
 *
 * Protocolo (texto, uma mensagem por datagrama):
 *
 *   plugin -> bridge
 *     S tick hp armor clip reserve arma x y z acao_atual hurt_ago_ms rtt_ms net_ms t_ms stale_drops
 *       |E userid hp dist_m visivel mirando_em_mim seen_ago_ms direcao   (um por inimigo vivo)
 *       |H tipo direcao dist_m ago_ms                                    (sons ouvidos nos últimos 3 s)
 *       - rtt_ms: MAIOR ida e volta entre as respostas recebidas desde o último estado
 *         (medida com GetEngineTime; -1 = nenhuma). O maior, e não o último, para um
 *         acúmulo de respostas atrasadas aparecer no bridge em vez de sumir.
 *       - net_ms: idem, rtt menos o tempo que o bridge disse ter gasto = rede de ida e
 *         volta + espera do callback no servidor.
 *       - t_ms: GetEngineTime() em ms no envio. O bridge devolve esse valor e usa para
 *         estimar o atraso de chegada com o relógio do servidor.
 *       - stale_drops: total de comandos descartados por chegarem velhos demais.
 *       - jev_fair_info 1: inimigo NÃO visível vai com hp -1 e dist_m -1 (sem wallhack).
 *       - seen_ago_ms: 0 se visível agora, 99999 se nunca visto neste round.
 *       - direcao: relativa ao olhar do bot: front, front_left, left, back_left,
 *         back, back_right, right, front_right (ou none). Para E, é a direção da
 *         posição atual (se visível) ou da última posição vista.
 *       - tipo de som: step, shot, reload, jump.
 *
 *   bridge -> plugin
 *     A <acao> [tick] [run|walk] [t_ms] [bridge_ms]
 *       - tick (opcional) = tick do estado que gerou a ação. O plugin usa para
 *         medir o rtt e para descartar respostas fora de ordem.
 *       - postura (opcional): walk = anda com shift (sem barulho de passo).
 *       - t_ms: eco do t_ms do estado; bridge_ms: tempo que o bridge levou do
 *         recebimento ao envio. Comando com tick mais velho que jev_max_cmd_age
 *         ticks é descartado (o bot fica no hold em vez de agir com info velha).
 *
 * Só para servidor local, com -insecure, contra quem topou jogar contra ele.
 *
 * Requer: SourceMod 1.10+, SDKTools, SDKHooks, sm-ext-socket (socket.inc).
 */

#pragma semicolon 1
#pragma newdecls required

#include <sourcemod>
#include <sdktools>
#include <sdkhooks>
#include <cstrike>
#include <socket>

#define PLUGIN_VERSION "0.3.0"

/* Duração de cada ação vinda do bridge. Sem comando novo, volta para hold. */
#define ACTION_DURATION      0.35
/* Intervalo entre tentativas de reconectar o socket. */
#define RECONNECT_DELAY      2.0
/* Unidades Hammer -> metros (1 unidade = 1 polegada). */
#define UNITS_TO_METERS      0.0254
/* cos(~10°): "mirando em mim" = produto escalar acima disso. */
#define AIMING_AT_ME_DOT     0.985
/* Se o alvo some por menos que isso, o tempo de reação não reinicia. */
#define TARGET_GRACE         0.20
/* Por quanto tempo continua mirando na última posição vista. */
#define LAST_SEEN_MEMORY     1.50
/* Troca de direção no strafe_shoot (A-D-A-D). */
#define STRAFE_PERIOD        0.18
/* Velocidade horizontal máxima para atirar durante o strafe_shoot. */
#define STRAFE_FIRE_SPEED    80.0
/* Intervalo mínimo entre "use <arma primária>". */
#define REEQUIP_INTERVAL     0.50

/* Audição. Alcances aproximados em unidades (1 m ~ 39 u), ajustáveis por jev_hear_scale. */
#define SOUND_MAX            4
#define SOUND_MEMORY         3.0
#define HEAR_STEP            1100.0
#define HEAR_SHOT            3000.0
#define HEAR_SHOT_SILENCED   900.0
#define HEAR_RELOAD          600.0
#define HEAR_JUMP            800.0
/* Erro de localização do som: até 8% da distância, para não virar radar perfeito. */
#define HEAR_POS_ERROR       0.08

enum
{
	SND_STEP = 0,
	SND_SHOT,
	SND_RELOAD,
	SND_JUMP
};

char g_sSoundNames[][] = { "step", "shot", "reload", "jump" };

/* ------------------------------------------------------------------------- */
/* Ações                                                                     */
/* ------------------------------------------------------------------------- */

enum
{
	ACT_HOLD = 0,
	ACT_PEEK_LEFT,
	ACT_PEEK_RIGHT,
	ACT_STRAFE_SHOOT,
	ACT_CROUCH_SPRAY,
	ACT_FALL_BACK,
	ACT_PUSH,
	ACT_RELOAD,
	ACT_FACE_SOUND,
	ACT_COUNT
};

char g_sActionNames[ACT_COUNT][] =
{
	"hold",
	"peek_left",
	"peek_right",
	"strafe_shoot",
	"crouch_spray",
	"fall_back",
	"push",
	"reload",
	"face_sound"
};

/* ------------------------------------------------------------------------- */
/* ConVars                                                                   */
/* ------------------------------------------------------------------------- */

ConVar g_cvEnabled;
ConVar g_cvHost;
ConVar g_cvPort;
ConVar g_cvReactionMs;
ConVar g_cvAimJitter;
ConVar g_cvAimSpeed;
ConVar g_cvMoveSpeed;
ConVar g_cvStateEvery;
ConVar g_cvFireCone;
ConVar g_cvRecoilControl;
ConVar g_cvDebug;
ConVar g_cvHud;
ConVar g_cvHearing;
ConVar g_cvHearScale;
ConVar g_cvFairInfo;
ConVar g_cvMaxCmdAge;
ConVar g_cvRecoilScale;   /* weapon_recoil_scale do jogo (pode não existir) */

/* ------------------------------------------------------------------------- */
/* Socket                                                                    */
/* ------------------------------------------------------------------------- */

Socket g_Socket = null;
bool   g_bSockReady = false;      /* Connect terminou e ainda não deu erro */
bool   g_bSockErrLogged = false;  /* evita spam: loga só a 1ª falha de uma sequência */
bool   g_bBridgeSeen = false;     /* já recebemos alguma resposta nesta conexão */
Handle g_hReconnectTimer = null;

/* ------------------------------------------------------------------------- */
/* Estado do bot                                                             */
/* ------------------------------------------------------------------------- */

int   g_iBot = 0;                 /* índice do cliente controlado (0 = nenhum) */
int   g_iAction = ACT_HOLD;
float g_fActionUntil = 0.0;
float g_fManualUntil = 0.0;       /* sm_jev bloqueia o bridge até este instante */
int   g_iLastAckTick = -1;        /* maior tick já aplicado (descarta fora de ordem) */
int   g_iLastRttMs = -1;
int   g_iLastNetMs = -1;          /* rtt - tempo do bridge = rede (ida+volta) + espera do callback */
int   g_iRttMaxWin = -1;          /* maiores valores desde o último estado enviado */
int   g_iNetMaxWin = -1;
int   g_iStaleDrops = 0;          /* comandos descartados por velhos (acumulado) */
float g_fLastHurt = -1.0;
int   g_iTickCounter = 0;
int   g_iFireToggle = 0;
float g_fLastReequip = 0.0;
bool  g_bWalk = false;            /* postura pedida junto com a ação atual */

/* Percepção (recalculada a cada frame em UpdatePerception). */
int   g_iEnemyCount = 0;
int   g_iEnemy[MAXPLAYERS + 1];
bool  g_bEnemyVisible[MAXPLAYERS + 1];
bool  g_bEnemyAiming[MAXPLAYERS + 1];
float g_fEnemyDist[MAXPLAYERS + 1];   /* em unidades */

int   g_iTarget = 0;              /* inimigo visível mais próximo (0 = nenhum) */
float g_fTargetSince = 0.0;       /* quando o alvo atual começou a ser visto (= g_fFirstSeen[alvo]) */
/* Tempo de reação por inimigo: só zera depois de TARGET_GRACE sem linha de visão.
 * Assim trocar de "mais próximo" entre dois inimigos visíveis não reinicia a reação. */
float g_fFirstSeen[MAXPLAYERS + 1];
float g_fLastVisible[MAXPLAYERS + 1];
float g_fLastVisiblePos[MAXPLAYERS + 1][3];

/* Sons ouvidos (anel com os SOUND_MAX mais recentes). */
int   g_iSoundType[SOUND_MAX];
float g_fSoundPos[SOUND_MAX][3];
float g_fSoundTime[SOUND_MAX];
int   g_iSoundNext = 0;
bool  g_bConnectWatch = false;
float g_fLastSeenPos[3];
float g_fLastSeenTime = -100.0;

/* Mira. */
float g_fViewAng[3];
bool  g_bViewInit = false;
float g_fDesiredAng[3];
bool  g_bHasDesired = false;
float g_fJitter[2];
float g_fJitterNext = 0.0;
float g_fFocusTmp[3];            /* alvo do face_sound (preenchido por GetFocusPoint) */

/* Spots de duelo (por mapa, persistidos em data/jev_bot_spots.txt). */
#define SPOT_BOT   0
#define SPOT_HUMAN 1
bool  g_bSpotSet[2];
float g_fSpotPos[2][3];
float g_fSpotAng[2][3];
char  g_sSpotsPath[PLATFORM_MAX_PATH];

public Plugin myinfo =
{
	name        = "Jev Bot",
	author      = "Joao Raul",
	description = "Bot de CS:GO controlado por um modelo de decisão (System One) via UDP",
	version     = PLUGIN_VERSION,
	url         = ""
};

/* ========================================================================= */
/* Ciclo de vida                                                             */
/* ========================================================================= */

public void OnPluginStart()
{
	g_cvEnabled       = CreateConVar("jev_enabled", "1", "Liga/desliga o controle do bot pelo Jev.", _, true, 0.0, true, 1.0);
	g_cvHost          = CreateConVar("jev_bridge_host", "127.0.0.1", "Host (IP) do bridge.");
	g_cvPort          = CreateConVar("jev_bridge_port", "27500", "Porta UDP do bridge.", _, true, 1.0, true, 65535.0);
	g_cvReactionMs    = CreateConVar("jev_reaction_ms", "220", "Tempo de reação (ms) entre ver o inimigo e poder atirar.", _, true, 0.0, true, 2000.0);
	g_cvAimJitter     = CreateConVar("jev_aim_jitter", "1.2", "Erro de mira aleatório (graus) em volta da cabeça.", _, true, 0.0, true, 15.0);
	g_cvAimSpeed      = CreateConVar("jev_aim_speed", "6.0", "Velocidade máxima de giro da mira (graus por tick).", _, true, 0.1, true, 180.0);
	g_cvMoveSpeed     = CreateConVar("jev_move_speed", "450", "Intensidade do input de movimento (0-450; o jogo limita pela arma).", _, true, 0.0, true, 450.0);
	g_cvStateEvery    = CreateConVar("jev_state_every", "4", "Envia o estado a cada N ticks (4 @ 64 tick = 16 Hz).", _, true, 1.0, true, 64.0);
	g_cvFireCone      = CreateConVar("jev_fire_cone", "2.0", "Só atira se o erro entre a mira atual e a desejada for menor que isso (graus).", _, true, 0.1, true, 30.0);
	g_cvRecoilControl = CreateConVar("jev_recoil_control", "0.6", "Quanto do recoil o bot compensa (0 = nada, 1 = perfeito).", _, true, 0.0, true, 1.0);
	g_cvDebug         = CreateConVar("jev_debug", "0", "1 = imprime estados enviados e ações recebidas no console do servidor.", _, true, 0.0, true, 1.0);
	g_cvHud           = CreateConVar("jev_hud", "0", "1 = mostra na tela (hint) a ação que o cérebro escolheu, bom para gravar vídeo.", _, true, 0.0, true, 1.0);
	g_cvHearing       = CreateConVar("jev_hearing", "1", "1 = o bot ouve passos, tiros, recarga e pulo dos inimigos.", _, true, 0.0, true, 1.0);
	g_cvHearScale     = CreateConVar("jev_hear_scale", "1.0", "Multiplica o alcance da audição (0.5 = meio surdo, 2 = ouvido de morcego).", _, true, 0.1, true, 5.0);
	g_cvMaxCmdAge     = CreateConVar("jev_max_cmd_age", "16", "Descarta comando do bridge cujo estado de origem é mais velho que N ticks (0 = nunca descarta).", _, true, 0.0, true, 1024.0);
	g_cvFairInfo      = CreateConVar("jev_fair_info", "1", "1 = não manda hp/distância de inimigo que o bot não está vendo (sem wallhack).", _, true, 0.0, true, 1.0);
	CreateConVar("jev_bot_version", PLUGIN_VERSION, "Versão do jev_bot", FCVAR_NOTIFY | FCVAR_DONTRECORD);

	AutoExecConfig(true, "jev_bot");

	g_cvHost.AddChangeHook(OnBridgeAddrChanged);
	g_cvPort.AddChangeHook(OnBridgeAddrChanged);
	g_cvEnabled.AddChangeHook(OnEnabledChanged);

	RegAdminCmd("sm_jev", Cmd_Jev, ADMFLAG_CHEATS, "sm_jev <acao> [segundos] — força uma ação (testa o plugin sem o bridge).");
	RegAdminCmd("sm_jev_spot", Cmd_JevSpot, ADMFLAG_CHEATS, "sm_jev_spot bot|human|clear — salva sua posição/ângulo como spawn do duelo.");

	HookEvent("player_spawn", Event_PlayerSpawn);
	HookEvent("player_hurt", Event_PlayerHurt);
	HookEvent("round_start", Event_RoundStart, EventHookMode_PostNoCopy);
	/* Audição. Se algum evento não existir no build do jogo, HookEventEx só devolve false. */
	if (!HookEventEx("player_footstep", Event_Footstep)) LogError("[jev] evento player_footstep não existe: sem passos");
	if (!HookEventEx("weapon_fire", Event_WeaponFire))   LogError("[jev] evento weapon_fire não existe: sem tiros");
	if (!HookEventEx("weapon_reload", Event_Reload))     LogError("[jev] evento weapon_reload não existe: sem recarga");
	if (!HookEventEx("player_jump", Event_Jump))         LogError("[jev] evento player_jump não existe: sem pulo");

	BuildPath(Path_SM, g_sSpotsPath, sizeof(g_sSpotsPath), "data/jev_bot_spots.txt");

	/* Carregado com o mapa já rodando (sm plugins reload): engancha quem já está no servidor. */
	for (int i = 1; i <= MaxClients; i++)
	{
		if (IsClientInGame(i))
		{
			OnClientPutInServer(i);
		}
	}
}

public void OnConfigsExecuted()
{
	/* Aqui as cvars já vieram do cfg/sourcemod/jev_bot.cfg. */
	Bridge_Connect();
}

public void OnMapStart()
{
	g_iBot = 0;
	ResetBotState();
	g_cvRecoilScale = FindConVar("weapon_recoil_scale");
	g_fLastReequip = 0.0;   /* GetGameTime() volta a ~0 em mapa novo */
	g_fJitterNext = 0.0;
	LoadSpots();
}

public void OnMapEnd()
{
	g_bConnectWatch = false;
	/* O timer de reconexão tem TIMER_FLAG_NO_MAPCHANGE: o SourceMod mata ele na
	 * troca de mapa, então só esquecemos o handle. */
	g_hReconnectTimer = null;
}

public void OnPluginEnd()
{
	Bridge_Close();
}

public void OnClientPutInServer(int client)
{
	SDKHook(client, SDKHook_WeaponCanSwitchTo, Hook_WeaponCanSwitchTo);
}

public void OnClientDisconnect(int client)
{
	if (client == g_iBot)
	{
		g_iBot = 0;
		ResetBotState();
	}
}

public void OnEnabledChanged(ConVar convar, const char[] oldValue, const char[] newValue)
{
	ResetBotState();
}

void ResetBotState()
{
	g_iAction = ACT_HOLD;
	g_fActionUntil = 0.0;
	g_fManualUntil = 0.0;
	g_iLastAckTick = -1;
	g_fLastHurt = -1.0;
	g_iTarget = 0;
	g_fLastSeenTime = -100.0;
	g_bViewInit = false;
	g_bHasDesired = false;
	g_iEnemyCount = 0;
	g_iTickCounter = 0;
	g_iLastRttMs = -1;
	g_bWalk = false;
	ResetMemory();
}

/* Esquece o que viu e ouviu (novo round, bot novo, mapa novo). */
void ResetMemory()
{
	for (int i = 0; i <= MaxClients; i++)
	{
		g_fFirstSeen[i] = -1.0;
		g_fLastVisible[i] = -100.0;
	}
	for (int k = 0; k < SOUND_MAX; k++)
	{
		g_fSoundTime[k] = -100.0;
	}
	g_iSoundNext = 0;
}

/* ========================================================================= */
/* Socket UDP (sm-ext-socket)                                                */
/* ========================================================================= */
/*
 * Para UDP a extensão só começa a receber depois de Connect(); Connect é
 * assíncrono e chama OnSockConnected quando termina. Um socket UDP
 * "conectado" também só aceita datagramas vindos do host:porta do bridge.
 *
 * Se o bridge não estiver rodando, o ICMP "port unreachable" volta como
 * erro de recebimento (Linux) ou como desconexão (Windows, WSAECONNRESET).
 * Nos dois casos fechamos o socket e tentamos de novo a cada RECONNECT_DELAY,
 * logando só a primeira falha de cada sequência.
 */

void Bridge_Connect()
{
	delete g_hReconnectTimer;   /* cancela reconexão pendente (e zera o handle) */
	Bridge_Close();
	g_iLastRttMs = -1;
	g_iLastNetMs = -1;
	g_iLastAckTick = -1;

	char host[64];
	g_cvHost.GetString(host, sizeof(host));
	int port = g_cvPort.IntValue;

	/* Natives em estilo função (SocketCreate/SocketConnect/SocketSend) em vez do
	 * methodmap Socket: funcionam tanto com o build do JoinedSenses quanto com
	 * o 3.0.1 clássico do fórum da AlliedModders. */
	g_Socket = SocketCreate(SOCKET_UDP, OnSockError);
	SocketConnect(g_Socket, OnSockConnected, OnSockReceive, OnSockDisconnected, host, port);

	/* Se o connect falhar sem chamar callback nenhum, o watchdog tenta de novo. */
	if (!g_bConnectWatch)
	{
		g_bConnectWatch = true;
		CreateTimer(5.0, Timer_ConnectWatchdog, _, TIMER_FLAG_NO_MAPCHANGE);
	}
}

public Action Timer_ConnectWatchdog(Handle timer)
{
	g_bConnectWatch = false;
	if (!g_bSockReady && g_hReconnectTimer == null)
	{
		LogBridgeProblem("connect não terminou em 5 s", 0);
		Bridge_ScheduleReconnect();
	}
	return Plugin_Stop;
}

void Bridge_Close()
{
	g_bSockReady = false;
	g_bBridgeSeen = false;
	if (g_Socket != null)
	{
		delete g_Socket;   /* fecha o handle (e o socket) */
		g_Socket = null;
	}
}

void Bridge_ScheduleReconnect()
{
	g_bSockReady = false;
	if (g_hReconnectTimer == null)
	{
		g_hReconnectTimer = CreateTimer(RECONNECT_DELAY, Timer_Reconnect, _, TIMER_FLAG_NO_MAPCHANGE);
	}
}

public Action Timer_Reconnect(Handle timer)
{
	g_hReconnectTimer = null;
	Bridge_Connect();   /* fecha o socket antigo fora do callback dele */
	return Plugin_Stop;
}

public void OnBridgeAddrChanged(ConVar convar, const char[] oldValue, const char[] newValue)
{
	g_bSockErrLogged = false;
	Bridge_Connect();
}

public void OnSockConnected(Socket socket, any arg)
{
	if (socket != g_Socket)
	{
		return;
	}
	g_bSockReady = true;
	if (g_cvDebug.BoolValue)
	{
		LogMessage("[jev] socket UDP pronto");
	}
}

public void OnSockDisconnected(Socket socket, any arg)
{
	if (socket != g_Socket)
	{
		return;
	}
	LogBridgeProblem("desconectado (provavelmente porta fechada: bridge não está rodando?)", 0);
	Bridge_ScheduleReconnect();
}

public void OnSockError(Socket socket, const int errorType, const int errorNum, any arg)
{
	if (socket != g_Socket)
	{
		return;
	}
	char what[48];
	switch (errorType)
	{
		case EMPTY_HOST:    strcopy(what, sizeof(what), "host vazio");
		case NO_HOST:       strcopy(what, sizeof(what), "host não encontrado");
		case CONNECT_ERROR: strcopy(what, sizeof(what), "erro no connect");
		case SEND_ERROR:    strcopy(what, sizeof(what), "erro no envio");
		case BIND_ERROR:    strcopy(what, sizeof(what), "erro no bind");
		case RECV_ERROR:    strcopy(what, sizeof(what), "erro no recebimento (bridge fora do ar?)");
		case LISTEN_ERROR:  strcopy(what, sizeof(what), "erro no listen");
		default:            strcopy(what, sizeof(what), "erro desconhecido");
	}
	LogBridgeProblem(what, errorNum);
	Bridge_ScheduleReconnect();
}

void LogBridgeProblem(const char[] what, int errorNum)
{
	if (g_bSockErrLogged)
	{
		return;
	}
	g_bSockErrLogged = true;
	char host[64];
	g_cvHost.GetString(host, sizeof(host));
	LogError("[jev] bridge %s:%d indisponível: %s (errno %d). Tentando de novo a cada %.0f s, sem repetir este aviso.",
		host, g_cvPort.IntValue, what, errorNum, RECONNECT_DELAY);
}

public void OnSockReceive(Socket socket, const char[] receiveData, const int dataSize, any arg)
{
	if (socket != g_Socket)
	{
		return;
	}

	if (!g_bBridgeSeen)
	{
		g_bBridgeSeen = true;
		if (g_bSockErrLogged)
		{
			LogMessage("[jev] bridge voltou a responder.");
		}
		g_bSockErrLogged = false;
	}

	/* receiveData pode não terminar em \0: copia no máximo dataSize bytes. */
	char msg[128];
	int n = dataSize + 1;
	if (n > sizeof(msg))
	{
		n = sizeof(msg);
	}
	strcopy(msg, n, receiveData);
	TrimString(msg);

	HandleBridgeMessage(msg);
}

void HandleBridgeMessage(const char[] msg)
{
	char parts[6][32];
	int count = ExplodeString(msg, " ", parts, sizeof(parts), sizeof(parts[]));
	if (count < 2 || !StrEqual(parts[0], "A"))
	{
		return;
	}

	int act = ActionFromName(parts[1]);
	if (act < 0)
	{
		return;
	}

	bool stale = false;
	if (count >= 3)
	{
		int tick = StringToInt(parts[2]);
		/* UDP pode entregar fora de ordem: ignora resposta a estado mais velho. */
		if (tick < g_iLastAckTick)
		{
			return;
		}
		g_iLastAckTick = tick;
		int ageTicks = GetGameTickCount() - tick;

		if (count >= 5)
		{
			/* RTT pelo relógio do motor: precisão abaixo do tick. O relógio é float32
			 * em segundos, então a precisão cai para ~1 ms depois de ~3 h de servidor. */
			g_iLastRttMs = RoundToNearest(GetEngineTime() * 1000.0 - StringToFloat(parts[4]));
			g_iLastNetMs = (count >= 6) ? g_iLastRttMs - StringToInt(parts[5]) : -1;
		}
		else
		{
			/* bridge antigo: RTT em ticks (resolução de 1 tick) */
			g_iLastRttMs = RoundToNearest(float(ageTicks) * GetTickInterval() * 1000.0);
			g_iLastNetMs = -1;
		}
		if (g_iLastRttMs < 0)
		{
			g_iLastRttMs = -1;   /* sem sentido (ex.: mapa trocou) */
			g_iLastNetMs = -1;
		}
		if (g_iLastNetMs < 0)
		{
			g_iLastNetMs = -1;
		}
		if (g_iLastRttMs > g_iRttMaxWin)
		{
			g_iRttMaxWin = g_iLastRttMs;
		}
		if (g_iLastNetMs > g_iNetMaxWin)
		{
			g_iNetMaxWin = g_iLastNetMs;
		}

		/* Comando baseado num estado velho demais: melhor não agir do que agir errado. */
		int maxAge = g_cvMaxCmdAge.IntValue;
		stale = maxAge > 0 && ageTicks > maxAge;
	}

	if (g_cvDebug.BoolValue)
	{
		PrintToServer("[jev] <- %s (rtt %d ms, rede %d ms%s)", msg, g_iLastRttMs, g_iLastNetMs,
			stale ? ", DESCARTADO: velho" : "");
	}
	if (stale)
	{
		g_iStaleDrops++;
		return;
	}

	/* sm_jev manual tem prioridade enquanto durar. */
	if (GetGameTime() < g_fManualUntil)
	{
		return;
	}
	bool walk = (count >= 4 && StrEqual(parts[3], "walk", false));
	SetAction(act, ACTION_DURATION, walk);
}

int ActionFromName(const char[] name)
{
	for (int i = 0; i < ACT_COUNT; i++)
	{
		if (StrEqual(name, g_sActionNames[i], false))
		{
			return i;
		}
	}
	return -1;
}

void SetAction(int act, float duration, bool walk = false)
{
	float now = GetGameTime();
	bool changed = (act != CurrentAction(now)) || (walk != CurrentWalk(now));
	g_iAction = act;
	g_bWalk = walk;
	g_fActionUntil = now + duration;
	if (changed && g_cvHud.BoolValue && g_iBot != 0)
	{
		ShowHud(act);
	}
}

bool CurrentWalk(float now)
{
	return now < g_fActionUntil && g_bWalk;
}

/* Hint no centro da tela de todo mundo (inclusive SourceTV/demos). */
void ShowHud(int act)
{
	int hp = IsClientInGame(g_iBot) ? GetClientHealth(g_iBot) : 0;
	PrintHintTextToAll("JULIA: %s%s\n%N | %d HP | rtt %d ms", g_sActionNames[act], g_bWalk ? " (walking)" : "",
		g_iBot, hp, g_iLastRttMs);
}

int CurrentAction(float now)
{
	return (now < g_fActionUntil) ? g_iAction : ACT_HOLD;
}

/* ========================================================================= */
/* Escolha do bot                                                            */
/* ========================================================================= */

bool IsControllableBot(int client)
{
	return client > 0 && client <= MaxClients && IsClientInGame(client)
		&& IsFakeClient(client) && !IsClientSourceTV(client) && !IsClientReplay(client);
}

void EnsureBot()
{
	if (IsControllableBot(g_iBot))
	{
		return;
	}
	g_iBot = 0;

	/* Prefere um bot no T (é o que o jev_duel.cfg monta); senão, qualquer um. */
	int fallback = 0;
	for (int i = 1; i <= MaxClients; i++)
	{
		if (!IsControllableBot(i))
		{
			continue;
		}
		if (GetClientTeam(i) == CS_TEAM_T)
		{
			g_iBot = i;
			break;
		}
		if (fallback == 0 && GetClientTeam(i) > CS_TEAM_SPECTATOR)
		{
			fallback = i;
		}
	}
	if (g_iBot == 0)
	{
		g_iBot = fallback;
	}
	if (g_iBot != 0)
	{
		ResetBotState();
		if (g_cvDebug.BoolValue)
		{
			PrintToServer("[jev] controlando %N", g_iBot);
		}
	}
}

/* ========================================================================= */
/* Loop principal: percepção + envio do estado                               */
/* ========================================================================= */

public void OnGameFrame()
{
	if (!g_cvEnabled.BoolValue)
	{
		return;
	}
	EnsureBot();
	if (g_iBot == 0 || !IsPlayerAlive(g_iBot))
	{
		return;
	}

	UpdatePerception(g_iBot);
	KeepPrimaryEquipped(g_iBot);

	if (++g_iTickCounter >= g_cvStateEvery.IntValue)
	{
		g_iTickCounter = 0;
		SendState(g_iBot);
	}
}

/* Filtro do trace: ignora o próprio bot. */
public bool Filter_IgnoreSelf(int entity, int contentsMask, any data)
{
	return entity != data;
}

/* Linha de visão de `from` até `to`. Visível se nada bloqueia ou se o
 * primeiro obstáculo é o próprio alvo. Smoke NÃO bloqueia (limitação). */
bool CanSee(int bot, const float from[3], const float to[3], int target)
{
	TR_TraceRayFilter(from, to, MASK_SHOT, RayType_EndPoint, Filter_IgnoreSelf, bot);
	if (!TR_DidHit())
	{
		return true;
	}
	return TR_GetEntityIndex() == target;
}

void GetChestPosition(int client, float out[3])
{
	float origin[3], eye[3];
	GetClientAbsOrigin(client, origin);
	GetClientEyePosition(client, eye);
	out[0] = origin[0];
	out[1] = origin[1];
	/* ~70% da altura entre o pé e o olho: funciona em pé e agachado. */
	out[2] = origin[2] + (eye[2] - origin[2]) * 0.70;
}

void UpdatePerception(int bot)
{
	float now = GetGameTime();
	float eye[3];
	GetClientEyePosition(bot, eye);
	int botTeam = GetClientTeam(bot);

	g_iEnemyCount = 0;
	int best = 0;
	float bestDist = 999999.0;

	for (int i = 1; i <= MaxClients; i++)
	{
		if (i == bot || !IsClientInGame(i) || !IsPlayerAlive(i))
		{
			continue;
		}
		int team = GetClientTeam(i);
		if (team <= CS_TEAM_SPECTATOR || team == botTeam)
		{
			continue;
		}

		float eEye[3], chest[3];
		GetClientEyePosition(i, eEye);
		GetChestPosition(i, chest);

		/* olho -> olho e olho -> peito */
		bool visible = CanSee(bot, eye, eEye, i) || CanSee(bot, eye, chest, i);
		float dist = GetVectorDistance(eye, eEye);

		/* "Mirando em mim": direção do olhar do inimigo · direção inimigo->bot. */
		bool aiming = false;
		if (visible)
		{
			float ang[3], fwd[3], toBot[3];
			GetClientEyeAngles(i, ang);
			GetAngleVectors(ang, fwd, NULL_VECTOR, NULL_VECTOR);
			MakeVectorFromPoints(eEye, eye, toBot);
			NormalizeVector(toBot, toBot);
			aiming = GetVectorDotProduct(fwd, toBot) > AIMING_AT_ME_DOT;
		}

		int k = g_iEnemyCount++;
		g_iEnemy[k] = i;
		g_bEnemyVisible[k] = visible;
		g_bEnemyAiming[k] = aiming;
		g_fEnemyDist[k] = dist;

		if (visible)
		{
			/* reação por inimigo; tolera ele "piscar" atrás de uma quina */
			if (g_fFirstSeen[i] < 0.0 || now - g_fLastVisible[i] > TARGET_GRACE)
			{
				g_fFirstSeen[i] = now;
			}
			g_fLastVisible[i] = now;
			g_fLastVisiblePos[i] = eEye;
		}

		if (visible && dist < bestDist)
		{
			bestDist = dist;
			best = i;
		}
	}

	if (best != g_iTarget && best != 0)
	{
		g_fJitterNext = 0.0;   /* alvo novo: sorteia erro de mira novo */
	}
	g_iTarget = best;
	if (best != 0)
	{
		g_fTargetSince = g_fFirstSeen[best];
	}

	if (g_iTarget != 0)
	{
		GetClientEyePosition(g_iTarget, g_fLastSeenPos);
		g_fLastSeenTime = now;
	}
}

void SendState(int bot)
{
	if (g_Socket == null || !g_bSockReady)
	{
		return;
	}

	float now = GetGameTime();

	int clip = -1, reserve = -1;
	char weaponName[32] = "none";
	int weapon = GetEntPropEnt(bot, Prop_Send, "m_hActiveWeapon");
	if (weapon > MaxClients && IsValidEntity(weapon))
	{
		GetEntityClassname(weapon, weaponName, sizeof(weaponName));
		ReplaceString(weaponName, sizeof(weaponName), "weapon_", "");
		clip = GetEntProp(weapon, Prop_Send, "m_iClip1");
		if (HasEntProp(weapon, Prop_Send, "m_iPrimaryReserveAmmoCount"))
		{
			reserve = GetEntProp(weapon, Prop_Send, "m_iPrimaryReserveAmmoCount");
		}
	}

	float origin[3];
	GetClientAbsOrigin(bot, origin);

	int hurtAgo = 99999;
	if (g_fLastHurt >= 0.0)
	{
		hurtAgo = RoundToFloor((now - g_fLastHurt) * 1000.0);
		if (hurtAgo > 99999)
		{
			hurtAgo = 99999;
		}
	}

	char buf[1024];
	FormatEx(buf, sizeof(buf), "S %d %d %d %d %d %s %.0f %.0f %.0f %s %d %d %d %d %d",
		GetGameTickCount(),
		GetClientHealth(bot),
		GetClientArmor(bot),
		clip,
		reserve,
		weaponName,
		origin[0], origin[1], origin[2],
		g_sActionNames[CurrentAction(now)],
		hurtAgo,
		g_iRttMaxWin,
		g_iNetMaxWin,
		RoundToFloor(GetEngineTime() * 1000.0),
		g_iStaleDrops);
	g_iRttMaxWin = -1;
	g_iNetMaxWin = -1;

	float eye[3];
	GetClientEyePosition(bot, eye);
	float yaw = BotYaw(bot);
	bool fair = g_cvFairInfo.BoolValue;

	char part[96], dir[16];
	for (int k = 0; k < g_iEnemyCount; k++)
	{
		int e = g_iEnemy[k];
		bool vis = g_bEnemyVisible[k];
		int seenAgo = 99999;
		if (vis)
		{
			seenAgo = 0;
		}
		else if (g_fLastVisible[e] >= 0.0)
		{
			seenAgo = RoundToFloor((now - g_fLastVisible[e]) * 1000.0);
			if (seenAgo > 99999)
			{
				seenAgo = 99999;
			}
		}
		if (seenAgo < 99999)
		{
			DirBucket(eye, yaw, g_fLastVisiblePos[e], dir, sizeof(dir));
		}
		else
		{
			strcopy(dir, sizeof(dir), "none");
		}

		int hp = GetClientHealth(e);
		float distM = g_fEnemyDist[k] * UNITS_TO_METERS;
		if (fair && !vis)
		{
			hp = -1;
			distM = -1.0;
		}
		FormatEx(part, sizeof(part), "|E %d %d %.1f %d %d %d %s",
			GetClientUserId(e), hp, distM,
			vis ? 1 : 0,
			g_bEnemyAiming[k] ? 1 : 0,
			seenAgo, dir);
		StrCat(buf, sizeof(buf), part);
	}

	/* Sons, do mais novo para o mais velho. */
	for (int n = 1; n <= SOUND_MAX; n++)
	{
		int k = (g_iSoundNext - n + SOUND_MAX) % SOUND_MAX;
		float ago = now - g_fSoundTime[k];
		if (ago < 0.0 || ago > SOUND_MEMORY)
		{
			continue;
		}
		DirBucket(eye, yaw, g_fSoundPos[k], dir, sizeof(dir));
		FormatEx(part, sizeof(part), "|H %s %s %.0f %d",
			g_sSoundNames[g_iSoundType[k]], dir,
			GetVectorDistance(eye, g_fSoundPos[k]) * UNITS_TO_METERS,
			RoundToFloor(ago * 1000.0));
		StrCat(buf, sizeof(buf), part);
	}

	SocketSend(g_Socket, buf);

	if (g_cvDebug.BoolValue)
	{
		PrintToServer("[jev] -> %s", buf);
	}
}

/* ========================================================================= */
/* Armas                                                                     */
/* ========================================================================= */

/* Armas que seguram IN_ATTACK. As outras alternam aperta/solta.
 * Obs.: no CS:GO algumas armas dividem classname (cz75a usa weapon_p250,
 * usp_silencer usa weapon_hkp2000, mp5sd usa weapon_mp7, revolver usa
 * weapon_deagle). Para o duelo de AK isso não importa. */
bool IsAutomatic(const char[] name)
{
	static const char autos[][] =
	{
		"ak47", "m4a1", "m4a1_silencer", "galilar", "famas", "aug", "sg556",
		"mac10", "mp9", "mp7", "mp5sd", "ump45", "p90", "bizon",
		"negev", "m249", "xm1014"
	};
	for (int i = 0; i < sizeof(autos); i++)
	{
		if (StrEqual(name, autos[i]))
		{
			return true;
		}
	}
	return false;
}

/* Não deixa a IA nativa do bot trocar para faca/pistola enquanto a primária
 * tiver munição. */
public Action Hook_WeaponCanSwitchTo(int client, int weapon)
{
	if (!g_cvEnabled.BoolValue || client != g_iBot)
	{
		return Plugin_Continue;
	}
	int primary = GetPlayerWeaponSlot(client, CS_SLOT_PRIMARY);
	if (primary == -1 || weapon == primary)
	{
		return Plugin_Continue;
	}
	/* primária sendo largada/trocada: deixa o jogo escolher outra arma */
	if (GetEntPropEnt(primary, Prop_Send, "m_hOwnerEntity") != client)
	{
		return Plugin_Continue;
	}
	/* C4 sempre pode (o jev_duel.cfg nem dá C4, mas em outros modos o bot precisa plantar) */
	if (weapon == GetPlayerWeaponSlot(client, CS_SLOT_C4))
	{
		return Plugin_Continue;
	}
	int clip = GetEntProp(primary, Prop_Send, "m_iClip1");
	int reserve = GetEntProp(primary, Prop_Send, "m_iPrimaryReserveAmmoCount");
	if (clip <= 0 && reserve <= 0)
	{
		return Plugin_Continue;   /* primária seca: pode ir para a pistola */
	}
	return Plugin_Handled;
}

/* Se a arma ativa não é a primária (ex.: nasceu com a pistola na mão),
 * pede para equipar a primária. */
void KeepPrimaryEquipped(int bot)
{
	float now = GetGameTime();
	if (now - g_fLastReequip < REEQUIP_INTERVAL)
	{
		return;
	}
	int primary = GetPlayerWeaponSlot(bot, CS_SLOT_PRIMARY);
	if (primary == -1)
	{
		return;
	}
	int active = GetEntPropEnt(bot, Prop_Send, "m_hActiveWeapon");
	if (active == primary)
	{
		return;
	}
	int clip = GetEntProp(primary, Prop_Send, "m_iClip1");
	int reserve = GetEntProp(primary, Prop_Send, "m_iPrimaryReserveAmmoCount");
	if (clip <= 0 && reserve <= 0)
	{
		return;
	}
	char cls[64];
	GetEntityClassname(primary, cls, sizeof(cls));
	FakeClientCommand(bot, "use %s", cls);
	g_fLastReequip = now;
}

/* ========================================================================= */
/* Mira                                                                      */
/* ========================================================================= */

float AngleNormalize(float a)
{
	while (a > 180.0)
	{
		a -= 360.0;
	}
	while (a < -180.0)
	{
		a += 360.0;
	}
	return a;
}

void AnglesTo(const float from[3], const float to[3], float out[3])
{
	float dir[3];
	MakeVectorFromPoints(from, to, dir);
	GetVectorAngles(dir, out);
	out[0] = AngleNormalize(out[0]);
	out[1] = AngleNormalize(out[1]);
	out[2] = 0.0;
}

float AngleError(const float a[3], const float b[3])
{
	float dp = AngleNormalize(a[0] - b[0]);
	float dy = AngleNormalize(a[1] - b[1]);
	return SquareRoot(dp * dp + dy * dy);
}

/* Atualiza g_fViewAng um passo na direção do ângulo desejado. */
void UpdateAim(int bot, float now, int act)
{
	if (!g_bViewInit)
	{
		GetClientEyeAngles(bot, g_fViewAng);
		g_fViewAng[0] = AngleNormalize(g_fViewAng[0]);
		g_fViewAng[1] = AngleNormalize(g_fViewAng[1]);
		g_fViewAng[2] = 0.0;
		g_bViewInit = true;
	}

	float eye[3];
	GetClientEyePosition(bot, eye);
	g_bHasDesired = false;

	if (g_iTarget != 0)
	{
		/* Sorteia um erro novo de tempos em tempos (não a cada tick, para não tremer). */
		if (now >= g_fJitterNext)
		{
			float j = g_cvAimJitter.FloatValue;
			g_fJitter[0] = GetRandomFloat(-j, j) * 0.6;   /* menos erro vertical */
			g_fJitter[1] = GetRandomFloat(-j, j);
			g_fJitterNext = now + 0.30;
		}
		float head[3];
		GetClientEyePosition(g_iTarget, head);
		AnglesTo(eye, head, g_fDesiredAng);
		g_fDesiredAng[0] += g_fJitter[0];
		g_fDesiredAng[1] += g_fJitter[1];
		g_bHasDesired = true;
	}
	else if (act == ACT_FACE_SOUND && GetFocusPoint(now, g_fFocusTmp))
	{
		/* Vira para o som mais recente (ou a última posição vista, se for mais nova). */
		AnglesTo(eye, g_fFocusTmp, g_fDesiredAng);
		g_bHasDesired = true;
	}
	else if (now - g_fLastSeenTime < LAST_SEEN_MEMORY)
	{
		/* Segura a mira onde viu o inimigo pela última vez. */
		AnglesTo(eye, g_fLastSeenPos, g_fDesiredAng);
		g_bHasDesired = true;
	}

	if (!g_bHasDesired)
	{
		return;
	}

	/* Compensação de recoil: no CS:GO o tiro sai em view + aimpunch * weapon_recoil_scale. */
	float rc = g_cvRecoilControl.FloatValue;
	if (rc > 0.0)
	{
		float punch[3];
		GetEntPropVector(bot, Prop_Send, "m_aimPunchAngle", punch);
		float scale = (g_cvRecoilScale != null) ? g_cvRecoilScale.FloatValue : 2.0;
		g_fDesiredAng[0] -= punch[0] * scale * rc;
		g_fDesiredAng[1] -= punch[1] * scale * rc;
	}

	if (g_fDesiredAng[0] > 89.0)
	{
		g_fDesiredAng[0] = 89.0;
	}
	else if (g_fDesiredAng[0] < -89.0)
	{
		g_fDesiredAng[0] = -89.0;
	}

	/* Passo suavizado: anda ~40% do caminho, limitado por jev_aim_speed. */
	float dp = AngleNormalize(g_fDesiredAng[0] - g_fViewAng[0]);
	float dy = AngleNormalize(g_fDesiredAng[1] - g_fViewAng[1]);
	float mag = SquareRoot(dp * dp + dy * dy);
	if (mag < 0.05)
	{
		g_fViewAng[0] = g_fDesiredAng[0];
		g_fViewAng[1] = g_fDesiredAng[1];
		return;
	}
	float step = mag * 0.40;
	if (step < 0.15)
	{
		step = 0.15;
	}
	float maxStep = g_cvAimSpeed.FloatValue;
	if (step > maxStep)
	{
		step = maxStep;
	}
	if (step >= mag)
	{
		g_fViewAng[0] = g_fDesiredAng[0];
		g_fViewAng[1] = g_fDesiredAng[1];
	}
	else
	{
		g_fViewAng[0] = AngleNormalize(g_fViewAng[0] + dp * step / mag);
		g_fViewAng[1] = AngleNormalize(g_fViewAng[1] + dy * step / mag);
	}
	g_fViewAng[2] = 0.0;
}

/* ========================================================================= */
/* Execução da ação                                                          */
/* ========================================================================= */

public Action OnPlayerRunCmd(int client, int &buttons, int &impulse, float vel[3], float angles[3],
	int &weapon, int &subtype, int &cmdnum, int &tickcount, int &seed, int mouse[2])
{
	if (!g_cvEnabled.BoolValue || client != g_iBot || !IsPlayerAlive(client))
	{
		return Plugin_Continue;
	}

	float now = GetGameTime();
	int act = CurrentAction(now);

	/* Arma ativa. */
	int clip = -1, reserve = 0;
	bool isAuto = false;
	int active = GetEntPropEnt(client, Prop_Send, "m_hActiveWeapon");
	if (active > MaxClients && IsValidEntity(active))
	{
		char name[32];
		GetEntityClassname(active, name, sizeof(name));
		ReplaceString(name, sizeof(name), "weapon_", "");
		isAuto = IsAutomatic(name);
		clip = GetEntProp(active, Prop_Send, "m_iClip1");
		if (HasEntProp(active, Prop_Send, "m_iPrimaryReserveAmmoCount"))
		{
			reserve = GetEntProp(active, Prop_Send, "m_iPrimaryReserveAmmoCount");
		}
	}

	/* 1) Mira (sempre roda, independente da ação). */
	UpdateAim(client, now, act);

	/* 2) Pode atirar? Alvo visível + tempo de reação + mira perto do ponto desejado. */
	bool ready = g_iTarget != 0 && (now - g_fTargetSince) * 1000.0 >= g_cvReactionMs.FloatValue;
	bool onTarget = g_bHasDesired && AngleError(g_fViewAng, g_fDesiredAng) <= g_cvFireCone.FloatValue;
	bool canFire = ready && onTarget && clip > 0 && act != ACT_RELOAD;

	/* 3) Movimento conforme a ação. */
	int newButtons = 0;
	float speed = g_cvMoveSpeed.FloatValue;
	float fwd = 0.0, side = 0.0;

	switch (act)
	{
		case ACT_PEEK_LEFT:
		{
			/* Anda para a esquerda até poder atirar; aí para (counter-strafe simples). */
			if (!canFire)
			{
				side = -speed;
			}
		}
		case ACT_PEEK_RIGHT:
		{
			if (!canFire)
			{
				side = speed;
			}
		}
		case ACT_STRAFE_SHOOT:
		{
			/* A-D-A-D; só atira quando a velocidade cai na troca de direção. */
			int phase = RoundToFloor(now / STRAFE_PERIOD) % 2;
			side = (phase == 0) ? speed : -speed;
			float v[3];
			GetEntPropVector(client, Prop_Data, "m_vecAbsVelocity", v);
			if (SquareRoot(v[0] * v[0] + v[1] * v[1]) > STRAFE_FIRE_SPEED)
			{
				canFire = false;
			}
		}
		case ACT_CROUCH_SPRAY:
		{
			newButtons |= IN_DUCK;
		}
		case ACT_FALL_BACK:
		{
			fwd = -speed;
		}
		case ACT_PUSH:
		{
			fwd = speed;
		}
		case ACT_RELOAD:
		{
			newButtons |= IN_RELOAD;
		}
	}

	/* 4) Recarrega sozinho com o pente vazio. */
	if (clip == 0 && reserve > 0)
	{
		newButtons |= IN_RELOAD;
		canFire = false;
	}

	/* 5) Tiro: automáticas seguram, semi-automáticas alternam. */
	if (canFire)
	{
		if (isAuto)
		{
			newButtons |= IN_ATTACK;
		}
		else
		{
			if ((g_iFireToggle++ % 2) == 0)
			{
				newButtons |= IN_ATTACK;
			}
		}
	}
	else
	{
		g_iFireToggle = 0;
	}

	/* Postura: andar com shift não faz barulho de passo. */
	if (CurrentWalk(now))
	{
		newButtons |= IN_SPEED;
	}

	/* Botões de movimento (para animação/servidor ficarem coerentes com vel). */
	if (fwd > 0.0)       newButtons |= IN_FORWARD;
	else if (fwd < 0.0)  newButtons |= IN_BACK;
	if (side > 0.0)      newButtons |= IN_MOVERIGHT;
	else if (side < 0.0) newButtons |= IN_MOVELEFT;

	/* Sobrescreve o usercmd inteiro: a IA nativa do bot é ignorada. */
	buttons = newButtons;
	vel[0] = fwd;
	vel[1] = side;
	vel[2] = 0.0;
	angles[0] = g_fViewAng[0];
	angles[1] = g_fViewAng[1];
	angles[2] = 0.0;
	weapon = 0;   /* nunca troca de arma pelo usercmd */

	/* Mantém os ângulos de olho do bot sincronizados com o usercmd. */
	TeleportEntity(client, NULL_VECTOR, g_fViewAng, NULL_VECTOR);

	return Plugin_Changed;
}

/* ========================================================================= */
/* Audição                                                                   */
/* ========================================================================= */

float BotYaw(int bot)
{
	if (g_bViewInit)
	{
		return g_fViewAng[1];
	}
	float ang[3];
	GetClientEyeAngles(bot, ang);
	return ang[1];
}

/* Direção de `pos` relativa ao olhar do bot, em 8 fatias de 45°.
 * No Source o yaw cresce para a ESQUERDA (anti-horário visto de cima). */
void DirBucket(const float eye[3], float yaw, const float pos[3], char[] out, int maxlen)
{
	float ang[3];
	AnglesTo(eye, pos, ang);
	float d = AngleNormalize(ang[1] - yaw);   /* + = esquerda, - = direita */
	if (d > -22.5 && d <= 22.5)        strcopy(out, maxlen, "front");
	else if (d > 22.5 && d <= 67.5)    strcopy(out, maxlen, "front_left");
	else if (d > 67.5 && d <= 112.5)   strcopy(out, maxlen, "left");
	else if (d > 112.5 && d <= 157.5)  strcopy(out, maxlen, "back_left");
	else if (d > -67.5 && d <= -22.5)  strcopy(out, maxlen, "front_right");
	else if (d > -112.5 && d <= -67.5) strcopy(out, maxlen, "right");
	else if (d > -157.5 && d <= -112.5) strcopy(out, maxlen, "back_right");
	else                               strcopy(out, maxlen, "back");
}

/* Ponto para onde o face_sound olha: o som mais novo ou a última posição vista, o que for mais recente. */
bool GetFocusPoint(float now, float out[3])
{
	float best = -1.0;
	for (int k = 0; k < SOUND_MAX; k++)
	{
		float ago = now - g_fSoundTime[k];
		if (ago >= 0.0 && ago <= SOUND_MEMORY && g_fSoundTime[k] > best)
		{
			best = g_fSoundTime[k];
			out = g_fSoundPos[k];
		}
	}
	if (g_fLastSeenTime > best && now - g_fLastSeenTime <= SOUND_MEMORY)
	{
		best = g_fLastSeenTime;
		out = g_fLastSeenPos;
	}
	return best >= 0.0;
}

/* Registra um som emitido por `source` se o bot estiver no alcance. */
void HearSound(int source, int type, float range)
{
	if (!g_cvEnabled.BoolValue || !g_cvHearing.BoolValue)
	{
		return;
	}
	int bot = g_iBot;
	if (bot == 0 || source == bot || !IsClientInGame(bot) || !IsPlayerAlive(bot))
	{
		return;
	}
	if (source <= 0 || source > MaxClients || !IsClientInGame(source) || !IsPlayerAlive(source))
	{
		return;
	}
	int team = GetClientTeam(source);
	if (team <= CS_TEAM_SPECTATOR || team == GetClientTeam(bot))
	{
		return;   /* só inimigos */
	}

	float eye[3], pos[3];
	GetClientEyePosition(bot, eye);
	GetClientEyePosition(source, pos);
	float dist = GetVectorDistance(eye, pos);
	if (dist > range * g_cvHearScale.FloatValue)
	{
		return;
	}

	/* Ouvido não é GPS: espalha a posição proporcionalmente à distância. */
	float err = dist * HEAR_POS_ERROR;
	pos[0] += GetRandomFloat(-err, err);
	pos[1] += GetRandomFloat(-err, err);

	int k = g_iSoundNext;
	g_iSoundType[k] = type;
	g_fSoundPos[k] = pos;
	g_fSoundTime[k] = GetGameTime();
	g_iSoundNext = (k + 1) % SOUND_MAX;

	if (g_cvDebug.BoolValue)
	{
		PrintToServer("[jev] ouviu %s de %N a %.0f m", g_sSoundNames[type], source, dist * UNITS_TO_METERS);
	}
}

/* O jogo não dispara player_footstep quando o jogador anda com shift ou agachado,
 * então andar silencioso já vale para o bot do mesmo jeito que vale para humanos. */
public void Event_Footstep(Event event, const char[] name, bool dontBroadcast)
{
	HearSound(GetClientOfUserId(event.GetInt("userid")), SND_STEP, HEAR_STEP);
}

public void Event_WeaponFire(Event event, const char[] name, bool dontBroadcast)
{
	/* "silenced" existe no weapon_fire do CS:GO; se faltar, GetBool devolve false. */
	float range = event.GetBool("silenced") ? HEAR_SHOT_SILENCED : HEAR_SHOT;
	HearSound(GetClientOfUserId(event.GetInt("userid")), SND_SHOT, range);
}

public void Event_Reload(Event event, const char[] name, bool dontBroadcast)
{
	HearSound(GetClientOfUserId(event.GetInt("userid")), SND_RELOAD, HEAR_RELOAD);
}

public void Event_Jump(Event event, const char[] name, bool dontBroadcast)
{
	HearSound(GetClientOfUserId(event.GetInt("userid")), SND_JUMP, HEAR_JUMP);
}

/* ========================================================================= */
/* Eventos                                                                   */
/* ========================================================================= */

public void Event_RoundStart(Event event, const char[] name, bool dontBroadcast)
{
	g_fActionUntil = 0.0;
	g_iTarget = 0;
	g_fLastSeenTime = -100.0;
	g_fLastHurt = -1.0;
	ResetMemory();
}

public void Event_PlayerSpawn(Event event, const char[] name, bool dontBroadcast)
{
	int userid = event.GetInt("userid");
	int client = GetClientOfUserId(userid);
	if (client == 0)
	{
		return;
	}
	if (client == g_iBot)
	{
		g_bViewInit = false;
		g_iAction = ACT_HOLD;
		g_fActionUntil = 0.0;
		g_fLastHurt = -1.0;
		g_iTarget = 0;
		g_fLastSeenTime = -100.0;
	}
	/* Espera um pouco: o jogo ainda posiciona o jogador depois do evento. */
	CreateTimer(0.1, Timer_ApplySpot, userid, TIMER_FLAG_NO_MAPCHANGE);
}

public void Event_PlayerHurt(Event event, const char[] name, bool dontBroadcast)
{
	int victim = GetClientOfUserId(event.GetInt("userid"));
	if (victim != 0 && victim == g_iBot)
	{
		g_fLastHurt = GetGameTime();
	}
}

public Action Timer_ApplySpot(Handle timer, any userid)
{
	int client = GetClientOfUserId(userid);
	if (client == 0 || !IsClientInGame(client) || !IsPlayerAlive(client))
	{
		return Plugin_Stop;
	}

	int which = -1;
	if (IsFakeClient(client))
	{
		EnsureBot();
		if (client == g_iBot)
		{
			which = SPOT_BOT;
		}
	}
	else
	{
		which = SPOT_HUMAN;   /* todo humano vai para o spot humano */
	}

	if (which != -1 && g_bSpotSet[which])
	{
		float zero[3];
		TeleportEntity(client, g_fSpotPos[which], g_fSpotAng[which], zero);
		if (which == SPOT_BOT)
		{
			g_fViewAng[0] = g_fSpotAng[which][0];
			g_fViewAng[1] = g_fSpotAng[which][1];
			g_fViewAng[2] = 0.0;
			g_bViewInit = true;
		}
	}
	return Plugin_Stop;
}

/* ========================================================================= */
/* Comandos                                                                  */
/* ========================================================================= */

public Action Cmd_Jev(int client, int args)
{
	if (args < 1)
	{
		ReplyToCommand(client, "[jev] uso: sm_jev <acao> [segundos=2] [walk]. Ações: hold, peek_left, peek_right, strafe_shoot, crouch_spray, fall_back, push, reload, face_sound");
		return Plugin_Handled;
	}

	char name[32];
	GetCmdArg(1, name, sizeof(name));
	int act = ActionFromName(name);
	if (act < 0)
	{
		ReplyToCommand(client, "[jev] ação desconhecida: %s", name);
		return Plugin_Handled;
	}

	float secs = 2.0;
	if (args >= 2)
	{
		char s[16];
		GetCmdArg(2, s, sizeof(s));
		secs = StringToFloat(s);
		if (secs <= 0.0)
		{
			secs = 2.0;
		}
	}

	EnsureBot();
	if (g_iBot == 0)
	{
		ReplyToCommand(client, "[jev] nenhum bot para controlar (bot_add_t?)");
		return Plugin_Handled;
	}
	if (!g_cvEnabled.BoolValue)
	{
		ReplyToCommand(client, "[jev] jev_enabled está 0");
		return Plugin_Handled;
	}

	bool walk = false;
	if (args >= 3)
	{
		char w[8];
		GetCmdArg(3, w, sizeof(w));
		walk = StrEqual(w, "walk", false);
	}

	SetAction(act, secs, walk);
	g_fManualUntil = GetGameTime() + secs;
	ReplyToCommand(client, "[jev] %N: %s%s por %.1f s (bridge ignorado nesse intervalo)", g_iBot, g_sActionNames[act],
		walk ? " andando" : "", secs);
	return Plugin_Handled;
}

public Action Cmd_JevSpot(int client, int args)
{
	char arg[16];
	if (args >= 1)
	{
		GetCmdArg(1, arg, sizeof(arg));
	}

	if (StrEqual(arg, "clear", false))
	{
		g_bSpotSet[SPOT_BOT] = false;
		g_bSpotSet[SPOT_HUMAN] = false;
		SaveSpots();
		ReplyToCommand(client, "[jev] spots deste mapa apagados");
		return Plugin_Handled;
	}

	int which = -1;
	if (StrEqual(arg, "bot", false))
	{
		which = SPOT_BOT;
	}
	else if (StrEqual(arg, "human", false))
	{
		which = SPOT_HUMAN;
	}
	if (which == -1)
	{
		ReplyToCommand(client, "[jev] uso: sm_jev_spot bot|human|clear");
		return Plugin_Handled;
	}
	if (client == 0 || !IsClientInGame(client) || !IsPlayerAlive(client))
	{
		ReplyToCommand(client, "[jev] use dentro do jogo, vivo, parado no ponto desejado");
		return Plugin_Handled;
	}

	GetClientAbsOrigin(client, g_fSpotPos[which]);
	GetClientEyeAngles(client, g_fSpotAng[which]);
	g_fSpotAng[which][2] = 0.0;
	g_fSpotPos[which][2] += 4.0;   /* um pouco acima do chão para não prender */
	g_bSpotSet[which] = true;
	SaveSpots();

	ReplyToCommand(client, "[jev] spot '%s' salvo em %.0f %.0f %.0f (vale a partir do próximo spawn; mp_restartgame 1)",
		arg, g_fSpotPos[which][0], g_fSpotPos[which][1], g_fSpotPos[which][2]);
	return Plugin_Handled;
}

/* ========================================================================= */
/* Persistência dos spots                                                    */
/* ========================================================================= */
/*
 * data/jev_bot_spots.txt:
 *   "jev_spots"
 *   {
 *       "de_dust2"
 *       {
 *           "bot"   { "pos" "x y z"  "ang" "p y r" }
 *           "human" { ... }
 *       }
 *   }
 */

static const char SPOT_KEYS[2][] = { "bot", "human" };

/* Mapas da workshop vêm como "workshop/123/de_x"; "/" vira separador de caminho
 * no KeyValues, então troca por "_". */
void GetSpotsMapKey(char[] map, int maxlen)
{
	GetCurrentMap(map, maxlen);
	ReplaceString(map, maxlen, "/", "_");
	ReplaceString(map, maxlen, "\\", "_");
}

void LoadSpots()
{
	g_bSpotSet[SPOT_BOT] = false;
	g_bSpotSet[SPOT_HUMAN] = false;

	KeyValues kv = new KeyValues("jev_spots");
	if (!kv.ImportFromFile(g_sSpotsPath))
	{
		delete kv;
		return;
	}

	char map[PLATFORM_MAX_PATH];
	GetSpotsMapKey(map, sizeof(map));
	if (kv.JumpToKey(map))
	{
		for (int w = 0; w < 2; w++)
		{
			if (kv.JumpToKey(SPOT_KEYS[w]))
			{
				kv.GetVector("pos", g_fSpotPos[w]);
				kv.GetVector("ang", g_fSpotAng[w]);
				g_bSpotSet[w] = true;
				kv.GoBack();
			}
		}
	}
	delete kv;
}

void SaveSpots()
{
	KeyValues kv = new KeyValues("jev_spots");
	kv.ImportFromFile(g_sSpotsPath);   /* mantém os outros mapas */

	char map[PLATFORM_MAX_PATH];
	GetSpotsMapKey(map, sizeof(map));
	kv.DeleteKey(map);

	if (g_bSpotSet[SPOT_BOT] || g_bSpotSet[SPOT_HUMAN])
	{
		kv.JumpToKey(map, true);
		for (int w = 0; w < 2; w++)
		{
			if (!g_bSpotSet[w])
			{
				continue;
			}
			kv.JumpToKey(SPOT_KEYS[w], true);
			kv.SetVector("pos", g_fSpotPos[w]);
			kv.SetVector("ang", g_fSpotAng[w]);
			kv.GoBack();
		}
	}

	kv.Rewind();
	if (!kv.ExportToFile(g_sSpotsPath))
	{
		LogError("[jev] não consegui salvar %s", g_sSpotsPath);
	}
	delete kv;
}
