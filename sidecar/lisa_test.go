package main

import (
	"bufio"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"fmt"
	"net"
	"net/http"
	"net/url"
	"testing"
	"time"
)

func TestTargetMatchesExactAndSuffix(t *testing.T) {
	hosts := map[string]bool{"pasarela-ident.clave.gob.es:443": true, ".registradores.org:443": true}
	cases := map[string]bool{
		"pasarela-ident.clave.gob.es:443": true,
		"otra.clave.gob.es:443":           false,
		"registradores.org:443":           true,
		"sso.registradores.org:443":       true,
		"a.b.registradores.org:443":       true,
		"sso.registradores.org:8443":      false,
		"registradores.org.evil.com:443":  false,
		"evilregistradores.org:443":       false,
	}
	for target, want := range cases {
		if got := targetMatches(hosts, target); got != want {
			t.Errorf("targetMatches(%q) = %v, quería %v", target, got, want)
		}
	}
}

func TestHostAllowedBySuffix(t *testing.T) {
	p := &proxy{allowedHosts: normalizeSuffixes([]string{".registradores.org", "clave.gob.es."})}
	cases := map[string]bool{
		"sede.registradores.org:443": true,
		"registradores.org:443":      true,
		"clave.gob.es:443":           true,
		"x.clave.gob.es:443":         true,
		"evilclave.gob.es:443":       false,
		"example.com:443":            false,
	}
	for target, want := range cases {
		if got := p.hostAllowed(target); got != want {
			t.Errorf("hostAllowed(%q) = %v, quería %v", target, got, want)
		}
	}
	if !(&proxy{}).hostAllowed("example.com:443") {
		t.Error("sin lista, cualquier destino vale")
	}
}

func TestPublicIP(t *testing.T) {
	cases := map[string]bool{
		"8.8.8.8": true, "2001:4860:4860::8888": true,
		"127.0.0.1": false, "::1": false, "10.0.2.2": false, "172.30.0.1": false, "192.168.1.1": false,
		"100.100.100.100": false, "169.254.1.1": false, "0.0.0.0": false, "fd7a:115c:a1e0::1": false,
	}
	for ip, want := range cases {
		if got := publicIP(net.ParseIP(ip)); got != want {
			t.Errorf("publicIP(%s) = %v, quería %v", ip, got, want)
		}
	}
}

func TestListenOutsideLoopbackNeedsPeers(t *testing.T) {
	if _, err := listenAddress("172.30.0.1:8790", false); err == nil {
		t.Error("sin direcciones permitidas solo vale loopback")
	}
	if addr, err := listenAddress("172.30.0.1:8790", true); err != nil || addr != "172.30.0.1:8790" {
		t.Errorf("con direcciones permitidas: %q, %v", addr, err)
	}
	if _, err := listenAddress("", true); err == nil {
		t.Error("con direcciones permitidas hace falta una dirección concreta")
	}
}

// peerProxy es el proxy como lo usa Lisa: sin contraseña, solo para 127.0.0.1 y unos dominios.
func peerProxy(t *testing.T, hosts ...string) (*proxy, string) {
	t.Helper()
	key, _ := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	desk := &fakeDesktop{}
	p, err := newProxy(inMessage{Type: "config", Key: pkcs8(t, key), AllowedPeers: []string{"127.0.0.1"}, AllowedHosts: hosts}, desk)
	if err != nil {
		t.Fatal(err)
	}
	p.configure(inMessage{Type: "session", ID: 1, Session: "lisa", Listen: "127.0.0.1:0"})
	ack, ok := desk.last(func(m outMessage) bool { return m.Type == "ack" })
	if !ok || ack.Error != "" {
		t.Fatalf("sin puerto: %+v", ack)
	}
	t.Cleanup(func() { p.endSession("lisa"); p.mitm.Close() })
	return p, fmt.Sprintf("127.0.0.1:%d", ack.Port)
}

func through(addr string) *http.Client {
	return &http.Client{
		Transport: &http.Transport{Proxy: http.ProxyURL(&url.URL{Scheme: "http", Host: addr})},
		Timeout:   10 * time.Second,
	}
}

func TestAllowedPeerNeedsNoPasswordButCannotReachInternalAddresses(t *testing.T) {
	up := newUpstream(t, 0)
	_, addr := peerProxy(t)
	resp, err := through(addr).Get("https://" + up.target + "/")
	if err == nil {
		resp.Body.Close()
		t.Fatal("el proxy no debe conectar con una dirección de loopback")
	}
	// No es un 407: la dirección del navegador basta para usar el proxy.
	conn, err := net.Dial("tcp", addr)
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	fmt.Fprintf(conn, "CONNECT %s HTTP/1.1\r\nHost: %s\r\n\r\n", up.target, up.target)
	reply, err := http.ReadResponse(bufioReader(conn), nil)
	if err != nil {
		t.Fatal(err)
	}
	if reply.StatusCode == http.StatusProxyAuthRequired || reply.StatusCode == http.StatusOK {
		t.Fatalf("CONNECT a loopback: %d", reply.StatusCode)
	}
}

func TestDisallowedDestinationIsRefused(t *testing.T) {
	_, addr := peerProxy(t, "registradores.org")
	conn, err := net.Dial("tcp", addr)
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	fmt.Fprint(conn, "CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\n\r\n")
	reply, err := http.ReadResponse(bufioReader(conn), nil)
	if err != nil {
		t.Fatal(err)
	}
	if reply.StatusCode != http.StatusForbidden {
		t.Fatalf("CONNECT a un dominio no permitido: %d", reply.StatusCode)
	}
}

func bufioReader(c net.Conn) *bufio.Reader { return bufio.NewReader(c) }
