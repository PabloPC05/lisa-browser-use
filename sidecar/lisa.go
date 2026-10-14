package main

// Cambios de Lisa sobre el sidecar de SpainAssist (MIT, commit 2de06d3; ver NOTICE).
//
// En SpainAssist el proxy vive en el mismo ordenador que el navegador y escucha en loopback.
// En Lisa, Chromium corre en un contenedor y el proxy en el servidor, así que:
//
//   - escucha en la puerta de enlace de la red del contenedor y solo atiende a las direcciones
//     de `allowedPeers` (la del contenedor). Con ellas no hace falta la contraseña por petición,
//     que Chromium no sabría dar sin que el ejecutor atienda la autenticación del proxy;
//   - solo acepta destinos de `allowedHosts` (sufijos de dominio), y nunca conecta con
//     direcciones privadas, de loopback ni de Tailscale: el proxy está en el servidor y, sin
//     esto, una página podría llegar a sus servicios internos;
//   - una entrada de `hosts` que empieza por "." vale para el dominio y todos sus subdominios,
//     porque no siempre se sabe de antemano qué servidor de una sede pide el certificado.

import (
	"errors"
	"fmt"
	"net"
	"net/http"
	"strings"
	"syscall"
)

// peerAllowed: si hay lista de direcciones permitidas, la conexión viene de una de ellas.
func (p *proxy) peerAllowed(r *http.Request) bool {
	if len(p.allowedPeers) == 0 {
		return false
	}
	host, _, err := net.SplitHostPort(r.RemoteAddr)
	if err != nil {
		return false
	}
	ip := net.ParseIP(host)
	for _, allowed := range p.allowedPeers {
		if ip != nil && ip.Equal(allowed) {
			return true
		}
	}
	return false
}

// hostAllowed: el destino está bajo uno de los sufijos permitidos. Sin lista, cualquiera.
func (p *proxy) hostAllowed(target string) bool {
	if len(p.allowedHosts) == 0 {
		return true
	}
	host, _, err := net.SplitHostPort(target)
	if err != nil {
		host = target
	}
	host = strings.TrimSuffix(strings.ToLower(host), ".")
	for _, suffix := range p.allowedHosts {
		if host == suffix || strings.HasSuffix(host, "."+suffix) {
			return true
		}
	}
	return false
}

func parsePeers(values []string) ([]net.IP, error) {
	var peers []net.IP
	for _, v := range values {
		ip := net.ParseIP(v)
		if ip == nil {
			return nil, fmt.Errorf("bad allowed peer %q", v)
		}
		peers = append(peers, ip)
	}
	return peers, nil
}

func normalizeSuffixes(values []string) []string {
	var out []string
	for _, v := range values {
		v = strings.Trim(strings.ToLower(strings.TrimSpace(v)), ".")
		if v != "" {
			out = append(out, v)
		}
	}
	return out
}

// listenAddress: en loopback siempre; fuera de loopback solo si hay direcciones permitidas.
func listenAddress(listen string, peersConfigured bool) (string, error) {
	if !peersConfigured {
		return loopback(listen)
	}
	if listen == "" {
		return "", errors.New("a listen address is required with allowed peers")
	}
	if _, _, err := net.SplitHostPort(listen); err != nil {
		return "", err
	}
	return listen, nil
}

var errNotPublic = errors.New("destination is not a public address")

// cgnat es 100.64.0.0/10: las direcciones de Tailscale.
var cgnat = &net.IPNet{IP: net.IPv4(100, 64, 0, 0), Mask: net.CIDRMask(10, 32)}

func publicIP(ip net.IP) bool {
	return !(ip.IsLoopback() || ip.IsPrivate() || ip.IsLinkLocalUnicast() || ip.IsLinkLocalMulticast() ||
		ip.IsMulticast() || ip.IsUnspecified() || ip.IsInterfaceLocalMulticast() || cgnat.Contains(ip))
}

// publicOnly se comprueba al conectar, con la dirección ya resuelta: una respuesta de DNS que
// apunte a la red interna no sirve para colarse.
func publicOnly(_, address string, _ syscall.RawConn) error {
	host, _, err := net.SplitHostPort(address)
	if err != nil {
		return err
	}
	ip := net.ParseIP(host)
	if ip == nil || !publicIP(ip) {
		return errNotPublic
	}
	return nil
}

// targetMatches: `target` ("host:puerto") es uno de `hosts`, o está bajo una entrada ".sufijo:puerto".
func targetMatches(hosts map[string]bool, target string) bool {
	if hosts[target] {
		return true
	}
	host, port, err := net.SplitHostPort(target)
	if err != nil {
		return false
	}
	for entry := range hosts {
		suffix, entryPort, err := net.SplitHostPort(entry)
		if err != nil || entryPort != port || !strings.HasPrefix(suffix, ".") {
			continue
		}
		if host == suffix[1:] || strings.HasSuffix(host, suffix) {
			return true
		}
	}
	return false
}
