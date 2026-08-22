/* Bric GitHub OIDC bridge for WPCode. Do not add a second <?php tag. */

if ( ! defined( 'ABSPATH' ) ) {
	return;
}

add_filter( 'determine_current_user', 'bric_bridge_oidc_determine_user', 30 );
add_filter( 'rest_authentication_errors', 'bric_bridge_oidc_authentication_error', 99 );

$GLOBALS['bric_bridge_oidc_error'] = null;

function bric_bridge_oidc_determine_user( $user_id ) {
	if ( $user_id ) {
		return $user_id;
	}

	$route = bric_bridge_oidc_rest_route();
	if ( ! bric_bridge_oidc_route_is_allowed( $route ) ) {
		return $user_id;
	}

	$token = '';
	if ( isset( $_SERVER['HTTP_X_BRIC_GITHUB_OIDC'] ) ) {
		$token = trim( wp_unslash( $_SERVER['HTTP_X_BRIC_GITHUB_OIDC'] ) );
	} else {
		$authorization = '';
		if ( isset( $_SERVER['HTTP_AUTHORIZATION'] ) ) {
			$authorization = trim( wp_unslash( $_SERVER['HTTP_AUTHORIZATION'] ) );
		} elseif ( isset( $_SERVER['REDIRECT_HTTP_AUTHORIZATION'] ) ) {
			$authorization = trim( wp_unslash( $_SERVER['REDIRECT_HTTP_AUTHORIZATION'] ) );
		}
		if ( preg_match( '/^Bearer\s+([A-Za-z0-9._-]+)$/', $authorization, $matches ) ) {
			$token = $matches[1];
		}
	}
	if ( ! preg_match( '/^[A-Za-z0-9._-]+$/', $token ) ) {
		return $user_id;
	}

	$claims = bric_bridge_oidc_validate_token( $token );
	if ( is_wp_error( $claims ) ) {
		$GLOBALS['bric_bridge_oidc_error'] = new WP_Error(
			'bric_oidc_rejected',
			$claims->get_error_code(),
			array( 'status' => 401 )
		);
		return $user_id;
	}

	$user = get_user_by( 'login', 'bric_automation' );
	if ( ! $user || ! in_array( 'author', (array) $user->roles, true ) ) {
		return $user_id;
	}

	wp_set_current_user( $user->ID );
	return $user->ID;
}

function bric_bridge_oidc_authentication_error( $result ) {
	if ( ! empty( $result ) ) {
		return $result;
	}
	return $GLOBALS['bric_bridge_oidc_error'] ?? $result;
}

function bric_bridge_oidc_rest_route() {
	if ( isset( $_GET['rest_route'] ) && is_string( $_GET['rest_route'] ) ) {
		return '/' . ltrim( wp_unslash( $_GET['rest_route'] ), '/' );
	}
	$uri  = isset( $_SERVER['REQUEST_URI'] ) ? wp_unslash( $_SERVER['REQUEST_URI'] ) : '';
	$path = wp_parse_url( $uri, PHP_URL_PATH );
	$mark = '/wp-json';
	$pos  = is_string( $path ) ? strpos( $path, $mark ) : false;
	return false === $pos ? '' : substr( $path, $pos + strlen( $mark ) );
}

function bric_bridge_oidc_route_is_allowed( $route ) {
	$method = isset( $_SERVER['REQUEST_METHOD'] ) ? strtoupper( $_SERVER['REQUEST_METHOD'] ) : '';
	if ( ! in_array( $method, array( 'GET', 'POST' ), true ) ) {
		return false;
	}
	if ( 'GET' === $method ) {
		return 1 === preg_match( '#^/wp/v2/(users/me|posts(?:/\d+)?|categories|media(?:/\d+)?)$#', $route );
	}
	return 1 === preg_match( '#^/wp/v2/(posts|media(?:/\d+)?)$#', $route );
}

function bric_bridge_oidc_validate_token( $token ) {
	$parts = explode( '.', $token );
	if ( 3 !== count( $parts ) ) {
		return new WP_Error( 'bric_oidc_format' );
	}
	$header  = json_decode( bric_bridge_oidc_b64url_decode( $parts[0] ), true );
	$claims  = json_decode( bric_bridge_oidc_b64url_decode( $parts[1] ), true );
	$sig     = bric_bridge_oidc_b64url_decode( $parts[2] );
	if ( ! is_array( $header ) || ! is_array( $claims ) || false === $sig ) {
		return new WP_Error( 'bric_oidc_decode' );
	}
	if ( 'RS256' !== ( $header['alg'] ?? '' ) || empty( $header['kid'] ) ) {
		return new WP_Error( 'bric_oidc_header' );
	}

	$key = bric_bridge_oidc_public_key( $header['kid'] );
	if ( is_wp_error( $key ) || ! function_exists( 'openssl_verify' ) ) {
		return new WP_Error( 'bric_oidc_key' );
	}
	$verified = openssl_verify( $parts[0] . '.' . $parts[1], $sig, $key, OPENSSL_ALGO_SHA256 );
	if ( 1 !== $verified ) {
		return new WP_Error( 'bric_oidc_signature' );
	}

	$now = time();
	$aud = $claims['aud'] ?? '';
	$aud_ok = is_array( $aud ) ? in_array( 'bric-wordpress-bridge', $aud, true ) : hash_equals( 'bric-wordpress-bridge', (string) $aud );
	$expected = array(
		'iss'           => 'https://token.actions.githubusercontent.com',
		'repository'    => 'insurancearenacom/bric-wordpress-bridge',
		'repository_id' => '1333923037',
		'ref'           => 'refs/heads/main',
		'environment'   => 'production',
		'workflow_ref'  => 'insurancearenacom/bric-wordpress-bridge/.github/workflows/publish-pending.yml@refs/heads/main',
	);
	if ( ! $aud_ok ) {
		return new WP_Error( 'bric_oidc_audience' );
	}
	$sub = (string) ( $claims['sub'] ?? '' );
	$allowed_subjects = array(
		'repo:insurancearenacom@11232036/bric-wordpress-bridge@1333923037:environment:production',
		'repo:insurancearenacom/bric-wordpress-bridge:environment:production',
	);
	if ( ! in_array( $sub, $allowed_subjects, true ) ) {
		return new WP_Error( 'bric_oidc_claim_sub' );
	}
	foreach ( $expected as $claim => $value ) {
		if ( ! isset( $claims[ $claim ] ) || ! hash_equals( $value, (string) $claims[ $claim ] ) ) {
			return new WP_Error( 'bric_oidc_claim_' . $claim );
		}
	}
	if ( ! in_array( $claims['event_name'] ?? '', array( 'push', 'workflow_dispatch' ), true ) ) {
		return new WP_Error( 'bric_oidc_event' );
	}
	if ( empty( $claims['exp'] ) || (int) $claims['exp'] < $now - 30 || (int) ( $claims['nbf'] ?? 0 ) > $now + 30 || (int) ( $claims['iat'] ?? 0 ) > $now + 30 ) {
		return new WP_Error( 'bric_oidc_time' );
	}
	return $claims;
}

function bric_bridge_oidc_b64url_decode( $value ) {
	$remainder = strlen( $value ) % 4;
	if ( $remainder ) {
		$value .= str_repeat( '=', 4 - $remainder );
	}
	return base64_decode( strtr( $value, '-_', '+/' ), true );
}

function bric_bridge_oidc_public_key( $kid ) {
	$jwks = get_transient( 'bric_bridge_github_oidc_jwks' );
	if ( ! is_array( $jwks ) || empty( $jwks['keys'] ) ) {
		$response = wp_remote_get(
			'https://token.actions.githubusercontent.com/.well-known/jwks',
			array( 'timeout' => 12, 'sslverify' => true )
		);
		if ( is_wp_error( $response ) || 200 !== wp_remote_retrieve_response_code( $response ) ) {
			return new WP_Error( 'bric_oidc_jwks_http' );
		}
		$jwks = json_decode( wp_remote_retrieve_body( $response ), true );
		if ( ! is_array( $jwks ) || empty( $jwks['keys'] ) ) {
			return new WP_Error( 'bric_oidc_jwks_json' );
		}
		set_transient( 'bric_bridge_github_oidc_jwks', $jwks, 6 * HOUR_IN_SECONDS );
	}

	foreach ( $jwks['keys'] as $jwk ) {
		if ( ! isset( $jwk['kid'] ) || ! hash_equals( (string) $kid, (string) $jwk['kid'] ) ) {
			continue;
		}
		if ( ! empty( $jwk['x5c'][0] ) ) {
			$certificate = "-----BEGIN CERTIFICATE-----\n" . chunk_split( $jwk['x5c'][0], 64, "\n" ) . "-----END CERTIFICATE-----\n";
			return openssl_pkey_get_public( $certificate );
		}
		if ( ! empty( $jwk['n'] ) && ! empty( $jwk['e'] ) ) {
			return openssl_pkey_get_public( bric_bridge_oidc_rsa_pem( $jwk['n'], $jwk['e'] ) );
		}
	}
	return new WP_Error( 'bric_oidc_kid' );
}

function bric_bridge_oidc_rsa_pem( $modulus, $exponent ) {
	$n = bric_bridge_oidc_b64url_decode( $modulus );
	$e = bric_bridge_oidc_b64url_decode( $exponent );
	$rsa = bric_bridge_oidc_der_sequence( bric_bridge_oidc_der_integer( $n ) . bric_bridge_oidc_der_integer( $e ) );
	$algorithm = hex2bin( '300d06092a864886f70d0101010500' );
	$spki = bric_bridge_oidc_der_sequence( $algorithm . "\x03" . bric_bridge_oidc_der_length( strlen( $rsa ) + 1 ) . "\x00" . $rsa );
	return "-----BEGIN PUBLIC KEY-----\n" . chunk_split( base64_encode( $spki ), 64, "\n" ) . "-----END PUBLIC KEY-----\n";
}

function bric_bridge_oidc_der_integer( $value ) {
	if ( '' === $value || ord( $value[0] ) > 0x7f ) {
		$value = "\x00" . $value;
	}
	return "\x02" . bric_bridge_oidc_der_length( strlen( $value ) ) . $value;
}

function bric_bridge_oidc_der_sequence( $value ) {
	return "\x30" . bric_bridge_oidc_der_length( strlen( $value ) ) . $value;
}

function bric_bridge_oidc_der_length( $length ) {
	if ( $length < 128 ) {
		return chr( $length );
	}
	$encoded = ltrim( pack( 'N', $length ), "\x00" );
	return chr( 0x80 | strlen( $encoded ) ) . $encoded;
}
